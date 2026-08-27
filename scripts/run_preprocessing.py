"""
FIM Database Preprocessing (reach-scale, multi-boundary-condition).

Standardizes flood maps for ONE NWM reach from multiple models into
database-ready GIS files + rating-curve CSVs + a rating-curve manifest.

Inputs it understands
---------------------
HAND depth rasters (from run_fimserve.py):
    <hand_dir>/HAND_<reach>_znone_f<cfs>cfs_depth.tif
    int16, millimetres, wet-only (nodata -9999). Downstream BC = none.

HEC-RAS1D extent library:
    <hecras_dir>/<reach>/z_<bc>/f_<cfs>.tif
    uint8 extent (1 = wet, 255 = nodata). Extent only (no depth).
    z_<wse>  -> downstream BC = known water-surface elevation
    z_nd     -> downstream BC = normal depth

Key idea
--------
A rating curve = one model under one downstream boundary condition
(1:1 with the DS BC). Its rows are the discharges. This script writes one
rating-curve CSV per (model, DS BC) and records them in
rating_curve_manifest.csv so populate_db can build:
    fim_source (per model) -> many rating_curves (per DS BC) -> flows.

Usage
-----
    cd scripts
    python run_preprocessing.py --config run_preprocessing.yaml
"""

import argparse
import glob
import math
import os
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import yaml
from rasterio.features import shapes
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject, Resampling
from scipy.ndimage import zoom as scipy_zoom
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union


MM_TO_FT = 0.0032808399          # millimetres -> feet
FIXED_NODATA = -99999            # single nodata for all aligned rasters
TARGET_CRS = "EPSG:4326"


# ─── Downstream-BC helpers ──────────────────────────────────────────────────
#
# zlabel <-> (type, value). Used both for parsing source folders and for
# building canonical filenames.

def zlabel_from_folder(z_folder):
    """'z_88_0' -> 'z88-0';  'z_nd' -> 'znd'."""
    if z_folder == "z_nd":
        return "znd"
    return "z" + z_folder[2:].replace("_", "-")


def parse_zlabel(zlabel):
    """Return (dsbc_type, dsbc_value) for a zlabel token."""
    if zlabel == "znone":
        return "none", None
    if zlabel == "znd":
        return "normal_depth", None
    # znumeric, e.g. 'z88-0' -> 88.0
    return "known_wse", float(zlabel[1:].replace("-", "."))


def bc_display_label(model, dsbc_type, dsbc_value, wse_unit="ft"):
    """Human-readable scenario label used later in the viz app legend."""
    if dsbc_type == "none":
        return model
    if dsbc_type == "normal_depth":
        return f"{model} | DS: Normal Depth"
    return f"{model} | DS: WSE {dsbc_value:g} {wse_unit}"


# ─── Step 1: Build the input manifest ───────────────────────────────────────
#
# One record per source flood map: (model, reach, zlabel, flow_cfs, product,
# path, source_kind). source_kind drives how we standardize it.

def build_input_manifest(reach_id, hand_dir, hecras_dir):
    records = []

    # HAND depth rasters
    hand_glob = os.path.join(hand_dir, f"HAND_{reach_id}_znone_f*cfs_depth.tif")
    for fp in sorted(glob.glob(hand_glob)):
        m = re.search(rf"HAND_{reach_id}_znone_f([0-9\-]+)cfs_depth\.tif$",
                      os.path.basename(fp))
        if not m:
            continue
        records.append({
            "model": "HAND",
            "reach": str(reach_id),
            "zlabel": "znone",
            "flow_cfs": _to_flow(m.group(1)),
            "product": "depth",
            "path": fp,
            "kind": "hand_depth",
        })

    # HEC-RAS1D extent library
    reach_dir = os.path.join(hecras_dir, str(reach_id))
    for z_path in sorted(glob.glob(os.path.join(reach_dir, "z_*"))):
        if not os.path.isdir(z_path):
            continue
        zlabel = zlabel_from_folder(os.path.basename(z_path))
        for fp in sorted(glob.glob(os.path.join(z_path, "f_*.tif"))):
            m = re.search(r"f_(\d+(?:\.\d+)?)\.tif$", os.path.basename(fp))
            if not m:
                continue
            records.append({
                "model": "HEC-RAS1D",
                "reach": str(reach_id),
                "zlabel": zlabel,
                "flow_cfs": _to_flow(m.group(1)),
                "product": "extent",
                "path": fp,
                "kind": "hecras_extent",
            })

    if not records:
        raise SystemExit(
            f"No input maps found for reach {reach_id} in {hand_dir} or "
            f"{hecras_dir}."
        )
    df = pd.DataFrame(records)
    print(f"Input manifest: {len(df)} maps "
          f"({(df.model=='HAND').sum()} HAND, "
          f"{(df.model=='HEC-RAS1D').sum()} HEC-RAS1D) across "
          f"{df.zlabel.nunique()} boundary condition(s).")
    return df


def _to_flow(s):
    v = float(s.replace("-", "."))
    return int(v) if v.is_integer() else v


def _flow_tag(flow_cfs):
    return str(int(flow_cfs)) if float(flow_cfs).is_integer() \
        else str(flow_cfs).replace(".", "-")


def canonical_name(model, reach, flow_cfs, zlabel, product, ext):
    return f"{model}_{reach}_{_flow_tag(flow_cfs)}_cfs_{zlabel}_{product}.{ext}"


# ─── Step 2: Align all rasters to a common grid, then derive extents ─────────
#
# Ported from the earlier notebook: every scenario raster is reprojected to
# EPSG:4326, resampled to the FINEST resolution, and written onto a single
# shared union grid (identical origin / resolution / size). This is what lets
# the viz app compare any two scenarios cell-for-cell without artifacts.
#
#   HAND       -> aligned DEPTH raster (mm -> ft, bilinear)
#   HEC-RAS1D  -> aligned BINARY wet/dry raster (extent-only, nearest)
#
# Extent vectors are then polygonized from the aligned rasters so vectors and
# rasters stay perfectly consistent.

def fix_nodata(data, original_nodata):
    """Fold declared nodata, NaN, non-positive, and overflow values into
    a single FIXED_NODATA. Depth/extent are strictly positive where valid."""
    mask = np.zeros(data.shape, dtype=bool)
    if original_nodata is not None and not np.isnan(original_nodata):
        mask |= (data == original_nodata)
    mask |= np.isnan(data)
    mask |= (data <= 0)
    mask |= (data > 1e6)
    data[mask] = FIXED_NODATA
    return data


def standardize(records, gis_dir):
    os.makedirs(gis_dir, exist_ok=True)
    aligned = _align_to_common_grid(records, gis_dir)
    return _rasters_to_extents(aligned, gis_dir)


def _align_to_common_grid(records, gis_dir):
    recs = list(records.itertuples(index=False))

    # First pass: reproject to 4326, clean, track finest resolution + a
    # HAND raster's origin as the grid-snap reference.
    reproj = []
    finest = np.inf
    ref_origin = None
    for rec in recs:
        with rasterio.open(rec.path) as src:
            t, w, h = calculate_default_transform(
                src.crs, TARGET_CRS, src.width, src.height, *src.bounds)
            dst = np.full((h, w), FIXED_NODATA, dtype=np.float32)
            is_depth = (rec.kind == "hand_depth")
            reproject(
                source=rasterio.band(src, 1), destination=dst,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=t, dst_crs=TARGET_CRS,
                src_nodata=src.nodata, dst_nodata=FIXED_NODATA,
                resampling=(Resampling.bilinear if is_depth
                            else Resampling.nearest),
            )
            if is_depth:
                valid = dst != FIXED_NODATA
                dst[valid] = dst[valid] * MM_TO_FT           # mm -> ft
                dst = fix_nodata(dst, FIXED_NODATA)
            else:
                dst = np.where(dst == 1, 1.0, FIXED_NODATA).astype(np.float32)
        finest = min(finest, abs(t.a))
        if ref_origin is None and is_depth:
            ref_origin = (t.c, t.f)
        reproj.append({"rec": rec, "data": dst, "transform": t,
                       "is_depth": is_depth})
    if ref_origin is None:
        ref_origin = (reproj[0]["transform"].c, reproj[0]["transform"].f)
    ref_x, ref_y = ref_origin

    # Resample all to finest resolution and accumulate the union extent.
    u_left, u_bottom, u_right, u_top = np.inf, np.inf, -np.inf, -np.inf
    for d in reproj:
        t, data = d["transform"], d["data"]
        scale = abs(t.a) / finest
        if abs(scale - 1.0) > 1e-6:
            order = 1 if d["is_depth"] else 0
            data = scipy_zoom(data, scale, order=order).astype(np.float32)
            t = t * t.scale(1.0 / scale, 1.0 / scale)
        if d["is_depth"]:
            data = fix_nodata(data, FIXED_NODATA)
        else:
            data = np.where(data == 1, 1.0, FIXED_NODATA).astype(np.float32)
        d["data"], d["transform"] = data, t
        h, w = data.shape
        left, top = t.c, t.f
        u_left = min(u_left, left)
        u_top = max(u_top, top)
        u_right = max(u_right, left + w * finest)
        u_bottom = min(u_bottom, top - h * finest)

    # Snap the union extent to the reference (HAND) grid origin.
    snap = lambda v, o: o + round((v - o) / finest) * finest
    u_left, u_right = snap(u_left, ref_x), snap(u_right, ref_x)
    u_top, u_bottom = snap(u_top, ref_y), snap(u_bottom, ref_y)
    uw = int(round((u_right - u_left) / finest))
    uh = int(round((u_top - u_bottom) / finest))
    utransform = from_origin(u_left, u_top, finest, finest)

    # Second pass: place each raster onto the shared union canvas and write.
    rows = []
    for d in reproj:
        rec, t, data = d["rec"], d["transform"], d["data"]
        col_off = int(round((t.c - u_left) / finest))
        row_off = int(round((u_top - t.f) / finest))
        canvas = np.full((uh, uw), FIXED_NODATA, dtype=np.float32)
        c0, r0 = max(col_off, 0), max(row_off, 0)
        c1 = min(col_off + data.shape[1], uw)
        r1 = min(row_off + data.shape[0], uh)
        if c1 > c0 and r1 > r0:
            canvas[r0:r1, c0:c1] = data[r0 - row_off:r1 - row_off,
                                        c0 - col_off:c1 - col_off]
        product = "depth" if d["is_depth"] else "extent"
        name = canonical_name(rec.model, rec.reach, rec.flow_cfs, rec.zlabel,
                              product, "tif")
        prof = dict(driver="GTiff", dtype="float32", width=uw, height=uh,
                    count=1, crs=TARGET_CRS, transform=utransform,
                    nodata=FIXED_NODATA, compress="LZW")
        with rasterio.open(os.path.join(gis_dir, name), "w", **prof) as out:
            out.write(canvas, 1)
        rows.append(dict(model=rec.model, reach=rec.reach, zlabel=rec.zlabel,
                         flow_cfs=rec.flow_cfs, raster_tif=name,
                         is_depth=d["is_depth"]))
    print(f"  aligned {len(rows)} rasters to a common grid "
          f"{uw}x{uh} @ {finest:.8f} deg")
    return pd.DataFrame(rows)


def _rasters_to_extents(aligned_df, gis_dir):
    rows = []
    for r in aligned_df.itertuples(index=False):
        gdf = _raster_to_extent(os.path.join(gis_dir, r.raster_tif))
        ext_name = canonical_name(r.model, r.reach, r.flow_cfs, r.zlabel,
                                  "extent", "shp")
        ext_path = os.path.join(gis_dir, ext_name)
        if gdf.empty:
            print(f"    (no wet cells for {ext_name}; writing empty extent)")
            gdf.to_file(ext_path)
        else:
            _dissolve_simplify(gdf).to_file(ext_path)
        rows.append(dict(model=r.model, reach=r.reach, zlabel=r.zlabel,
                         flow_cfs=r.flow_cfs, extent_shp=ext_name,
                         raster_tif=r.raster_tif))
    return pd.DataFrame(rows)


def _raster_to_extent(raster_path):
    """Polygonize the wet cells (> 0, not nodata) of an aligned raster."""
    with rasterio.open(raster_path) as src:
        a = src.read(1)
        mask = (a != src.nodata) & (a > 0) & ~np.isnan(a)
        geoms = [
            {"properties": {"value": 1}, "geometry": g}
            for g, v in shapes(mask.astype(np.uint8), mask=mask,
                               transform=src.transform)
        ]
        crs = src.crs
    return gpd.GeoDataFrame.from_features(geoms, crs=crs) if geoms \
        else gpd.GeoDataFrame(geometry=[], crs=crs)


def _dissolve_simplify(gdf, tol_factor=1.5):
    metric = _pick_metric_crs(gdf)
    gdf_m = gdf.to_crs(metric)
    merged = unary_union(gdf_m.geometry)
    tol = max(_median_edge_len(merged) * tol_factor, 0.0)
    simple = merged.simplify(tol, preserve_topology=True).buffer(0)
    simple = _remove_holes(simple)
    out = gpd.GeoDataFrame(geometry=[simple], crs=metric)
    return out.to_crs(TARGET_CRS)


def _pick_metric_crs(gdf):
    try:
        lonlat = gdf if (gdf.crs and gdf.crs.is_geographic) else gdf.to_crs(4326)
        c = lonlat.union_all().centroid
        cx, cy = float(c.x), float(c.y)
        if -170 <= cx <= -50 and 5 <= cy <= 85:
            return "EPSG:5070"
        zone = int(math.floor((cx + 180) / 6) + 1)
        return f"EPSG:{32600 + zone}" if cy >= 0 else f"EPSG:{32700 + zone}"
    except Exception:
        return "EPSG:3857"


def _median_edge_len(geom):
    lens = []

    def ring(poly):
        xy = np.asarray(poly.exterior.coords)
        return np.sqrt(((xy[1:] - xy[:-1]) ** 2).sum(axis=1)).tolist()

    if isinstance(geom, Polygon):
        lens += ring(geom)
    elif isinstance(geom, MultiPolygon):
        for p in geom.geoms:
            lens += ring(p)
    return float(np.median(lens)) if lens else 0.0


def _remove_holes(g):
    if isinstance(g, Polygon):
        return Polygon(g.exterior)
    if isinstance(g, MultiPolygon):
        cleaned = [Polygon(p.exterior) for p in g.geoms if not p.is_empty]
        if len(cleaned) > 1:
            return MultiPolygon(cleaned)
        return cleaned[0] if cleaned else g
    return g


# ─── Step 3: Rating-curve CSVs + manifest ───────────────────────────────────

def write_rating_curves(std_df, defaults, output_dir):
    os.makedirs(output_dir, exist_ok=True)
    manifest = []
    # HAND (dsbc none) is the sensible default rating curve to display.
    for (model, zlabel), grp in std_df.groupby(["model", "zlabel"]):
        dsbc_type, dsbc_value = parse_zlabel(zlabel)

        rows = []
        for r in grp.sort_values("flow_cfs").itertuples(index=False):
            rows.append({
                "Flow": r.flow_cfs,
                "Depth": "",
                "ReturnPeriod": "",
                "DownstreamWSE": dsbc_value if dsbc_value is not None else "",
                "VectorExtent": r.extent_shp or "",
                # Every scenario carries its aligned raster in the DepthRaster
                # slot so the vis JSON gets a raster_file for on-the-fly
                # comparison (HAND = depth-ft; HEC-RAS1D = binary wet/dry).
                "DepthRaster": r.raster_tif or "",
                "WSERaster": "",
                "VelocityRaster": "",
                "BoundaryVector": "",
            })
        rc_df = pd.DataFrame(rows, columns=[
            "Flow", "Depth", "ReturnPeriod", "DownstreamWSE", "VectorExtent",
            "DepthRaster", "WSERaster", "VelocityRaster", "BoundaryVector",
        ])
        csv_name = f"{model}__{zlabel}_ratingcurve.csv"
        rc_df.to_csv(os.path.join(output_dir, csv_name), index=False)

        manifest.append({
            "model": model,
            "dsbc_type": dsbc_type,
            "dsbc_value": dsbc_value if dsbc_value is not None else "",
            "dsbc_label": bc_display_label(
                model, dsbc_type, dsbc_value,
                defaults.get("depth_unit", "ft")),
            "csv_file": csv_name,
            "is_primary": int(dsbc_type in ("none", "normal_depth")),
        })
        print(f"  rating curve: {csv_name}  ({len(rc_df)} flows, "
              f"BC={dsbc_type})")

    man_df = pd.DataFrame(manifest)
    man_path = os.path.join(output_dir, "rating_curve_manifest.csv")
    man_df.to_csv(man_path, index=False)
    print(f"  manifest: {man_path} ({len(man_df)} rating curves)")
    return man_df


# ─── Step 4: FIM source metadata, HUC8, feature IDs ─────────────────────────

SOFTWARE_MAP = {"HAND": "HAND", "HEC-RAS1D": "HEC-RAS1D"}


def write_fim_source_csv(models, model_meta, output_path):
    rows = []
    for model in models:
        meta = model_meta.get(model, {})
        rows.append({
            "FIMSourceName": model,
            "CoordinateReference": TARGET_CRS,
            "EntityName": meta.get("entity_name", ""),
            "EntityContactEmail": meta.get("entity_email", ""),
            "VersionNumber": meta.get("version", ""),
            "YearCreated": meta.get("year", ""),
            "EventDate": meta.get("event_date", ""),
            "AdditionalModelNotes": meta.get("notes", ""),
            "Software": SOFTWARE_MAP.get(model, "Others"),
        })
    pd.DataFrame(rows, columns=[
        "FIMSourceName", "CoordinateReference", "EntityName",
        "EntityContactEmail", "VersionNumber", "YearCreated",
        "EventDate", "AdditionalModelNotes", "Software",
    ]).to_csv(output_path, index=False)
    print(f"  FIM source CSV: {output_path} ({len(rows)} model(s))")


def write_huc8_csv(huc, output_path):
    pd.DataFrame({"HUC8": [str(huc).zfill(8)]}).to_csv(output_path, index=False)


def write_feature_ids_csv(reach_id, lat, lon, output_path):
    pd.DataFrame({
        "feature_id": [int(reach_id)],
        "latitude": [lat],
        "longitude": [lon],
    }).to_csv(output_path, index=False)


def reach_centroid(gis_dir, reach_id):
    """Approximate reach location = centroid of its HAND extent(s)."""
    shps = glob.glob(os.path.join(gis_dir, f"HAND_{reach_id}_*_extent.shp"))
    if not shps:
        shps = glob.glob(os.path.join(gis_dir, f"*_{reach_id}_*_extent.shp"))
    if not shps:
        return None, None
    gdf = gpd.read_file(shps[0]).to_crs(4326)
    if gdf.empty:
        return None, None
    c = gdf.union_all().centroid
    return round(float(c.y), 5), round(float(c.x), 5)


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(config_path):
    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Reach-scale FIM preprocessing")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    cfg = load_config(args.config)
    reach_id = str(cfg["reach_id"]).strip()
    huc = str(cfg["huc8"]).strip().zfill(8)
    hand_dir = cfg.get("hand_dir", "../All_Outputs/Models/HAND")
    hecras_dir = cfg.get("hecras_dir", "../HECRAS-1D")
    output_dir = cfg.get("output_dir", "../All_Outputs/Processed")
    gis_dir = os.path.join(output_dir, "final_gis_files")
    defaults = cfg.get("model_defaults", {})
    model_meta = cfg.get("model_metadata", {})

    os.makedirs(output_dir, exist_ok=True)

    print("\n>>> Step 1: Building input manifest...")
    manifest = build_input_manifest(reach_id, hand_dir, hecras_dir)

    print("\n>>> Step 2: Standardizing maps (extent vectors + HAND depth)...")
    std_df = standardize(manifest, gis_dir)

    print("\n>>> Step 3: Writing rating-curve CSVs + manifest...")
    write_rating_curves(std_df, defaults, output_dir)

    print("\n>>> Step 4: Writing FIM source / HUC8 / feature-ID metadata...")
    models = sorted(std_df["model"].unique())
    write_fim_source_csv(models, model_meta,
                         os.path.join(output_dir, "FIM_input_data.csv"))
    write_huc8_csv(huc, os.path.join(output_dir, "HUC8.csv"))

    lat = cfg.get("reach_latitude")
    lon = cfg.get("reach_longitude")
    if lat is None or lon is None:
        lat, lon = reach_centroid(gis_dir, reach_id)
    write_feature_ids_csv(reach_id, lat, lon,
                          os.path.join(output_dir, "feature_IDs.csv"))

    # run_populate_db.yaml is a static, hand-maintained config (not generated
    # here) — it stays the same across runs for a given reach.
    print(f"\nDone. Outputs in: {output_dir}/")


if __name__ == "__main__":
    main()
