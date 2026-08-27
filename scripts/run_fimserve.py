"""
FIMServe (reach-scale): generate HAND FIM for a single NWM reach.

Given one NWM reach ID and its HUC8, this:
  1. downloads the HUC8 HAND data (FIMserv),
  2. derives the discharge list from the matching HEC-RAS1D reach folder
     (so the HAND flows line up 1:1 with the HEC-RAS1D flows),
  3. runs OWP HAND FIM for each discharge (only the target reach is wet),
  4. clips each depth raster to the reach's HAND catchment,
  5. writes parseable outputs to the HAND models folder.

HAND is anchored on discharge ONLY (downstream boundary condition = "none"),
so exactly one HAND depth map is produced per unique discharge.

IMPORTANT: FIMserv resolves its working dirs (code/, data/inputs/, output/)
relative to the current working directory, so run this from the `scripts/`
folder:

    cd scripts
    python run_fimserve.py --config run_fimserve.yaml
"""

import argparse
import glob
import os
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.mask import mask

import fimserve as fm


# cubic feet per second -> cubic meters per second
CFS_TO_CMS = 0.028316846592

# Uniform nodata for the final HAND depth product: everything that is not a
# positive (wet) depth is set to this single value. The dry evaluation area
# is recoverable later from the reach catchment polygon.
NODATA = -9999


# ─── Step 1: Derive the discharge list from the HEC-RAS1D reach folder ───────
#
# Layout expected:  <hecras_dir>/<reach_id>/z_<bc>/f_<flow_cfs>.tif
# We take the UNION of distinct f_<flow> values across every z_* folder,
# because different downstream BCs carry different flow subsets and HAND
# should cover all of them.

def derive_flows_from_hecras(hecras_dir, reach_id):
    reach_dir = os.path.join(hecras_dir, str(reach_id))
    if not os.path.isdir(reach_dir):
        raise FileNotFoundError(
            f"HEC-RAS reach folder not found: {reach_dir}"
        )

    flows = set()
    pattern = os.path.join(reach_dir, "z_*", "f_*.tif")
    for fp in glob.glob(pattern):
        m = re.search(r"f_(\d+(?:\.\d+)?)\.tif$", os.path.basename(fp))
        if m:
            flows.add(float(m.group(1)))

    flows = sorted(flows)
    if not flows:
        raise ValueError(
            f"No f_*.tif flow files found under {reach_dir}/z_*/"
        )
    print(f"Derived {len(flows)} unique flows (cfs) for reach {reach_id}: {flows}")
    return flows


# ─── Step 2: Write per-flow discharge CSVs for FIMserv ──────────────────────
#
# FIMserv globs data/inputs/*<huc>*.csv, so the HUC MUST be in the filename.
# The output depth raster is named after the CSV basename, so we encode
# reach + flow here. discharge column must be in CMS.

def write_flow_csvs(reach_id, huc, flows_cfs, data_dir):
    os.makedirs(data_dir, exist_ok=True)
    written = []
    for flow_cfs in flows_cfs:
        flow_cms = flow_cfs * CFS_TO_CMS
        flow_tag = _fmt_flow(flow_cfs)
        csv_name = f"HAND_{reach_id}_f{flow_tag}_{huc}.csv"
        csv_path = os.path.join(data_dir, csv_name)
        pd.DataFrame(
            {"feature_id": [int(reach_id)], "discharge": [round(flow_cms, 6)]}
        ).to_csv(csv_path, index=False)
        written.append((flow_cfs, csv_path))
        print(f"  wrote {csv_name}  ({flow_cfs} cfs -> {flow_cms:.4f} cms)")
    return written


def _fmt_flow(flow_cfs):
    """Integer flows print without a decimal; keep decimals otherwise."""
    if float(flow_cfs).is_integer():
        return str(int(flow_cfs))
    return str(flow_cfs).replace(".", "-")


# ─── Step 3: Locate the reach's HAND catchment polygon ──────────────────────
#
# The OWP HAND data downloaded to output/flood_<huc>/<huc>/ contains
# crosswalked catchment polygons (HydroID <-> feature_id). We union all
# catchment polygons whose feature_id equals the target reach.

def find_reach_catchment(huc, reach_id, output_dir):
    huc_dir = os.path.join(output_dir, f"flood_{huc}", str(huc))
    candidates = glob.glob(
        os.path.join(huc_dir, "**", "*catchments*crosswalked*.gpkg"),
        recursive=True,
    )
    if not candidates:
        candidates = glob.glob(
            os.path.join(huc_dir, "**", "*catchments*.gpkg"), recursive=True
        )
    if not candidates:
        print(f"  WARNING: no catchment gpkg found under {huc_dir}")
        return None

    reach_str = str(reach_id)
    parts = []
    for gpkg in candidates:
        try:
            gdf = gpd.read_file(gpkg)
        except Exception as e:  # noqa: BLE001
            print(f"  (could not read {os.path.basename(gpkg)}: {e})")
            continue
        col = _pick_feature_col(gdf.columns)
        if col is None:
            continue
        hit = gdf[gdf[col].astype(str).str.split(".").str[0] == reach_str]
        if not hit.empty:
            parts.append(hit)

    if not parts:
        print(
            f"  WARNING: reach {reach_id} not found in any catchment gpkg; "
            "depth rasters will not be clipped."
        )
        return None

    merged = pd.concat(parts, ignore_index=True)
    catchment = gpd.GeoDataFrame(merged, crs=parts[0].crs)
    print(
        f"  found {len(catchment)} catchment polygon(s) for reach {reach_id} "
        f"(crs {catchment.crs})"
    )
    return catchment


def _pick_feature_col(cols):
    lut = {c.lower(): c for c in cols}
    for name in ("feature_id", "featureid", "comid", "nwm_feature_id"):
        if name in lut:
            return lut[name]
    return None


# ─── Step 4: Clip depth rasters to the reach catchment ──────────────────────

def clip_depths_to_reach(reach_id, catchment, inundation_dir, final_dir):
    os.makedirs(final_dir, exist_ok=True)
    depth_files = sorted(glob.glob(os.path.join(inundation_dir, "*_depth.tif")))
    if not depth_files:
        print(f"  no *_depth.tif found in {inundation_dir}")
        return []

    written = []
    for depth_fp in depth_files:
        flow_tag = _extract_flow_tag(os.path.basename(depth_fp))
        out_name = f"HAND_{reach_id}_znone_f{flow_tag}cfs_depth.tif"
        out_fp = os.path.join(final_dir, out_name)

        with rasterio.open(depth_fp) as src:
            if catchment is not None:
                geom = catchment.to_crs(src.crs).geometry
                out_img, out_transform = mask(
                    src, geom, crop=True, nodata=NODATA
                )
                meta = src.meta.copy()
                meta.update(
                    height=out_img.shape[1],
                    width=out_img.shape[2],
                    transform=out_transform,
                )
            else:
                out_img = src.read()
                meta = src.meta.copy()

        # Uniform nodata: keep only positive (wet) depths; fold dry (0) and
        # outside-catchment cells into a single nodata value.
        out_img = np.where(out_img > 0, out_img, NODATA).astype(meta["dtype"])
        meta.update(nodata=NODATA, compress="lzw")

        with rasterio.open(out_fp, "w", **meta) as dst:
            dst.write(out_img)
        written.append(out_fp)
        print(f"  saved {out_name}")
    return written


def _extract_flow_tag(depth_basename):
    """Pull the f<flow> token back out of a FIMserv depth filename."""
    m = re.search(r"_f([0-9\-]+)_", depth_basename)
    return m.group(1) if m else "NA"


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(config_path):
    import yaml

    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Generate reach-scale HAND FIM matching HEC-RAS1D flows"
    )
    parser.add_argument("--config", required=True, help="Path to YAML config")
    args = parser.parse_args()

    cfg = load_config(args.config)

    reach_id = str(cfg["reach_id"]).strip()
    huc = str(cfg["huc8"]).strip().zfill(8)
    hecras_dir = cfg["hecras_dir"]
    version = cfg.get("hand_version")  # e.g. "4.5", "4.8", or None (latest)

    output_dir = cfg.get("output_dir", "output")
    data_dir = cfg.get("csv_dir", os.path.join("data", "inputs"))
    final_dir = cfg.get("final_output_dir", "../All_Outputs/Models/HAND")

    print(f"\n>>> Reach {reach_id} | HUC {huc}")

    # 1. Download HUC8 HAND data
    print("\n>>> Step 1: Downloading HUC8 HAND data...")
    if version:
        fm.DownloadHUC8(huc, version=version)
    else:
        fm.DownloadHUC8(huc)

    # 2. Derive flows from HEC-RAS1D and write discharge CSVs
    print("\n>>> Step 2: Deriving flows from HEC-RAS1D and writing CSVs...")
    flows_cfs = derive_flows_from_hecras(hecras_dir, reach_id)
    write_flow_csvs(reach_id, huc, flows_cfs, data_dir)

    # 3. Run HAND FIM (produces *_depth.tif per discharge CSV)
    print("\n>>> Step 3: Running HAND FIM...")
    fm.runOWPHANDFIM(huc, depth=True)

    # 4. Locate the reach catchment and clip
    print("\n>>> Step 4: Clipping depth rasters to reach catchment...")
    catchment = find_reach_catchment(huc, reach_id, output_dir)
    inundation_dir = os.path.join(output_dir, f"flood_{huc}", f"{huc}_inundation")
    outputs = clip_depths_to_reach(reach_id, catchment, inundation_dir, final_dir)

    print(f"\nDone. {len(outputs)} HAND depth map(s) written to: {final_dir}/")


if __name__ == "__main__":
    main()
