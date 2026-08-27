"""
Populate FIM Database.

Creates/populates a SQLite database with FIM source metadata, HUC8 data,
rating curves, and file references. Optionally uploads to HydroShare.

Usage:
    python run_populate_db.py --config run_populate_db.yaml
"""

import argparse
import glob
import json
import os
import shutil
from datetime import datetime

import numpy as np
import pandas as pd
import sqlite3
import yaml


# ─── Database schema ────────────────────────────────────────────────────────

DBML_SCRIPT = """
Table fim_source {
  FIMSourceID integer [pk, increment]
  FIMSourceName text
  CoordinateReference text
  PrimaryRatingCurveID integer
  ResponsibleEntityID integer
  ModelTypeID integer
  VersionNumber tinytext
  YearCreated smallint
  EventDate datetime
  AdditionalModelNotes text
}

Table responsible_entities {
  ResponsibleEntityID integer [pk, increment]
  EntityName text
  EntityContactEmail text
}

Table feature_ids_fim_source {
  FeatureID integer
  RatingCurveID integer
  FimSourceID integer
  ReferenceNetwork text
}

Table model_type {
  ModelTypeID smallint [pk, increment]
  Software text
}

Table flows {
  RatingCurveID integer
  Flow float(2)
  Depth float(2)
  ReturnPeriod smallint
  DownstreamWSE float(2)
  FloodExtentVectorID integer
  DepthRasterID integer
  WSERasterID integer
  VelocityRasterID integer
  BoundaryVectorID integer
}

Table files {
  FileID integer [pk, increment]
  FileName text
}

Table rating_curves {
  RatingCurveID integer [pk, increment]
  RatingCurve text
  FlowUnit tinytext
  DepthUnit tinytext
  DownstreamBCType text
  DownstreamBCValue float
}

Table fim_source_huc8 {
  FIMSourceID integer
  HUCNumber integer
}
"""

MODEL_TYPES = {
    "HEC-RAS1D": 0, "HEC-RAS2D": 1, "HEC-RAS1D/2D_Combo": 2,
    "SRH-2D": 3, "FIER": 4, "AutoRoute": 5, "HAND": 6,
    "TRITON": 7, "Satellite_Observations": 8,
    "Surveyed_Flood_Extents": 9, "Nencarta": 10, "Others": 11,
}

MODEL_TYPE_DF = pd.DataFrame({
    "ModelTypeID": list(MODEL_TYPES.values()),
    "Software": list(MODEL_TYPES.keys()),
})


def insert_data(conn, table_name, df):
    df.to_sql(table_name, conn, if_exists="append", index=False)


def bc_display_label(model, dsbc_type, dsbc_value, wse_unit="ft"):
    """Scenario label shown in the viz app legend (model + downstream BC)."""
    if dsbc_type in (None, "", "none"):
        return model
    if dsbc_type == "normal_depth":
        return f"{model} | DS: Normal Depth"
    return f"{model} | DS: WSE {float(dsbc_value):g} {wse_unit}"


# ─── Step 1: Create or connect to database ──────────────────────────────────

def create_database(db_path, db_type):
    if db_type == "new":
        if os.path.isfile(db_path):
            raise ValueError(
                f"Database '{db_path}' already exists. "
                "Set database.type to 'existing' or delete it."
            )
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        table_statements = [
            s.strip() for s in DBML_SCRIPT.split("Table") if s.strip()
        ]
        for stmt in table_statements:
            name = stmt.split("{")[0].strip()
            columns = stmt.split("{")[1].split("}")[0].strip()
            columns = ", ".join(
                [col.strip() for col in columns.split("\n") if col.strip()]
            )
            cursor.execute(f"CREATE TABLE {name} ({columns})")
        conn.commit()
        print(f"Created new database: {db_path}")
    else:
        if not os.path.isfile(db_path):
            raise ValueError(f"Database '{db_path}' not found.")
        conn = sqlite3.connect(db_path)
        print(f"Connected to existing database: {db_path}")

    return conn


# ─── Step 2: Populate fim_source table ──────────────────────────────────────

def populate_fim_source(conn, fim_source_csv):
    input_data_df = pd.read_csv(fim_source_csv)

    duplicates = []
    seen = set()
    existing = set(
        pd.read_sql_query(
            "SELECT FIMSourceName FROM fim_source", conn
        )["FIMSourceName"].tolist()
    )
    for name in input_data_df["FIMSourceName"]:
        if name in seen or name in existing:
            duplicates.append(name)
        else:
            seen.add(name)
    if duplicates:
        raise ValueError(f"Duplicate FIM Source Names: {', '.join(duplicates)}")

    # Populate model_type table if empty
    count = pd.read_sql_query(
        "SELECT COUNT(*) FROM model_type", conn
    ).iloc[0, 0]
    if count == 0:
        insert_data(conn, "model_type", MODEL_TYPE_DF)

    num_sources = len(input_data_df)
    start_id = pd.read_sql_query(
        "SELECT COUNT(*) FROM fim_source", conn
    ).iloc[0, 0]
    fim_source_ids = list(range(start_id, start_id + num_sources))

    # Fill blank entity fields so downstream logic doesn't break
    input_data_df["EntityName"] = input_data_df["EntityName"].fillna("")
    input_data_df["EntityContactEmail"] = input_data_df["EntityContactEmail"].fillna("")

    # Responsible entities
    starting_entity = pd.read_sql_query(
        "SELECT COUNT(*) FROM responsible_entities", conn
    ).iloc[0, 0]
    for entity_name in input_data_df["EntityName"].unique():
        if entity_name == "":
            input_data_df.loc[
                input_data_df["EntityName"] == entity_name,
                "ResponsibleEntityID",
            ] = 0
            continue
        existing_entity = pd.read_sql_query(
            f"SELECT ResponsibleEntityID FROM responsible_entities "
            f"WHERE EntityName LIKE '{entity_name}'",
            conn,
        )
        if existing_entity.empty:
            input_data_df.loc[
                input_data_df["EntityName"] == entity_name,
                "ResponsibleEntityID",
            ] = starting_entity
            starting_entity += 1
        else:
            input_data_df.loc[
                input_data_df["EntityName"] == entity_name,
                "ResponsibleEntityID",
            ] = existing_entity.iloc[0, 0]

    # Model type IDs
    for software in input_data_df["Software"].unique():
        model_type_id = pd.read_sql_query(
            f"SELECT ModelTypeID FROM model_type WHERE Software LIKE '{software}'",
            conn,
        ).iloc[0, 0]
        input_data_df.loc[
            input_data_df["Software"] == software, "ModelTypeID"
        ] = model_type_id

    input_data_df = input_data_df.astype(
        {"ModelTypeID": int, "ResponsibleEntityID": int}
    )
    input_data_df["FIMSourceID"] = fim_source_ids

    # Parse event dates
    for fmt in ["%m/%d/%Y", "%m/%d/%y"]:
        try:
            input_data_df["EventDate"] = pd.to_datetime(
                input_data_df["EventDate"], format=fmt
            )
            break
        except ValueError:
            pass

    fim_source_df = input_data_df[
        [
            "FIMSourceID", "FIMSourceName", "CoordinateReference",
            "ResponsibleEntityID", "ModelTypeID", "VersionNumber",
            "YearCreated", "EventDate", "AdditionalModelNotes",
        ]
    ]
    insert_data(conn, "fim_source", fim_source_df)

    # Responsible entities table
    re_df = input_data_df[
        ["ResponsibleEntityID", "EntityName", "EntityContactEmail"]
    ]
    unique_entities = re_df.groupby("ResponsibleEntityID").head(1)
    existing_ids = set(
        pd.read_sql_query(
            "SELECT ResponsibleEntityID FROM responsible_entities", conn
        )["ResponsibleEntityID"].tolist()
    )
    to_insert = unique_entities[
        ~unique_entities["ResponsibleEntityID"].isin(existing_ids)
    ]
    insert_data(conn, "responsible_entities", to_insert)

    print(f"Added {num_sources} FIM source(s). IDs: {fim_source_ids}")
    return fim_source_ids


# ─── Step 3: Add HUC8 data for a FIM source ─────────────────────────────────

def add_huc8_data(conn, huc8_csv, fim_source_id):
    huc8_list = (
        pd.read_csv(huc8_csv)["HUC8"].astype(str).str.zfill(8).tolist()
    )

    huc8_df = pd.DataFrame({
        "FIMSourceID": [fim_source_id] * len(huc8_list),
        "HUCNumber": huc8_list,
    })

    existing = pd.read_sql_query(
        "SELECT HUCNumber, FIMSourceID FROM fim_source_huc8", conn
    )
    existing["HUCNumber"] = existing["HUCNumber"].astype(str).str.zfill(8)

    merged = huc8_df.merge(
        existing, how="inner", on=["HUCNumber", "FIMSourceID"]
    )

    if not merged.empty:
        huc8_df = huc8_df[~huc8_df["HUCNumber"].isin(merged["HUCNumber"])]

    if not huc8_df.empty:
        insert_data(conn, "fim_source_huc8", huc8_df)
        print(f"  HUC8 data added for FIMSourceID {fim_source_id}")
    else:
        print(f"  HUC8 data already exists for FIMSourceID {fim_source_id}")


# ─── Step 4: Add rating curve for a FIM source ──────────────────────────────

def add_rating_curve(conn, model_cfg, fim_source_id):
    rc_file = model_cfg["rating_curve_file"]
    feature_id_file = model_cfg["feature_ids_file"]
    is_primary = model_cfg.get("primary_rating_curve", True)
    flow_unit = model_cfg.get("flow_unit", "cfs")
    depth_unit = model_cfg.get("depth_unit", "ft")
    nwm_version = str(model_cfg.get("nwm_reference_version", "3.0"))
    dsbc_type = model_cfg.get("dsbc_type", "none")
    dsbc_value = model_cfg.get("dsbc_value", None)
    if dsbc_value in ("", None) or pd.isna(dsbc_value):
        dsbc_value = None
    else:
        dsbc_value = float(dsbc_value)

    cur = conn.cursor()

    flows_df = pd.read_csv(rc_file)
    feature_ids = pd.read_csv(feature_id_file)["feature_id"].tolist()

    # Get next rating curve ID
    rc_count = pd.read_sql_query(
        "SELECT COUNT(*) FROM rating_curves", conn
    ).iloc[0, 0]
    if rc_count == 0:
        rc_number = 0
    else:
        rc_number = (
            pd.read_sql_query(
                "SELECT MAX(RatingCurveID) FROM rating_curves", conn
            ).iloc[0, 0]
            + 1
        )

    # Set primary rating curve
    if is_primary:
        cur.execute(
            f"UPDATE fim_source SET PrimaryRatingCurveID = {rc_number} "
            f"WHERE FimSourceID = '{fim_source_id}'"
        )

    # Add to rating_curves table
    rc_df = pd.DataFrame([{
        "RatingCurveID": rc_number,
        "RatingCurve": rc_file,
        "FlowUnit": flow_unit,
        "DepthUnit": depth_unit,
        "DownstreamBCType": dsbc_type,
        "DownstreamBCValue": dsbc_value,
    }])
    insert_data(conn, "rating_curves", rc_df)

    # Add to flows table with file IDs
    flows_df["RatingCurveID"] = rc_number
    file_id_counter = pd.read_sql_query(
        "SELECT COUNT(*) FROM files", conn
    ).iloc[0, 0]

    for idx, row in flows_df.iterrows():
        flows_df.at[idx, "FloodExtentVectorID"] = file_id_counter
        file_id_counter += 1

        if not pd.isna(row.get("DepthRaster")):
            flows_df.at[idx, "DepthRasterID"] = file_id_counter
            file_id_counter += 1
        if not pd.isna(row.get("WSERaster")):
            flows_df.at[idx, "WSERasterID"] = file_id_counter
            file_id_counter += 1
        if not pd.isna(row.get("VelocityRaster")):
            flows_df.at[idx, "VelocityRasterID"] = file_id_counter
            file_id_counter += 1
        if not pd.isna(row.get("BoundaryVector")):
            flows_df.at[idx, "BoundaryVectorID"] = file_id_counter
            file_id_counter += 1

    for col in [
        "DepthRasterID", "WSERasterID", "VelocityRasterID", "BoundaryVectorID"
    ]:
        if col not in flows_df.columns:
            flows_df[col] = np.nan

    flows_data_df = flows_df.copy()

    flow_columns = [
        "RatingCurveID", "Flow", "Depth", "ReturnPeriod",
        "DownstreamWSE",
        "FloodExtentVectorID", "DepthRasterID", "WSERasterID",
        "VelocityRasterID", "BoundaryVectorID",
    ]
    existing_cols = [c for c in flow_columns if c in flows_df.columns]
    insert_data(conn, "flows", flows_df[existing_cols])

    # Add to files table
    files_dict = {"FileID": [], "FileName": []}
    for _, row in flows_data_df.iterrows():
        files_dict["FileID"].append(row["FloodExtentVectorID"])
        files_dict["FileName"].append(row["VectorExtent"])

        for id_col, name_col in [
            ("DepthRasterID", "DepthRaster"),
            ("WSERasterID", "WSERaster"),
            ("VelocityRasterID", "VelocityRaster"),
            ("BoundaryVectorID", "BoundaryVector"),
        ]:
            if id_col in row and pd.notna(row[id_col]):
                files_dict["FileID"].append(row[id_col])
                files_dict["FileName"].append(row[name_col])

    insert_data(conn, "files", pd.DataFrame(files_dict))

    # Add to feature_ids_fim_source table
    feature_ids_dict = {
        "FeatureID": feature_ids,
        "RatingCurveID": [rc_number] * len(feature_ids),
        "FIMSourceID": [fim_source_id] * len(feature_ids),
        "ReferenceNetwork": [nwm_version] * len(feature_ids),
    }
    insert_data(conn, "feature_ids_fim_source", pd.DataFrame(feature_ids_dict))

    conn.commit()
    print(f"  Rating curve '{rc_file}' added (RatingCurveID={rc_number})")


# ─── Step 5: Generate filenames CSV and rename files ─────────────────────────

def generate_filenames_and_rename(conn, db_file, gis_dir, output_dir="."):
    cursor = conn.cursor()

    cursor.execute("SELECT filename FROM files")
    filenames = [row[0] for row in cursor.fetchall()]
    cursor.execute("SELECT RatingCurve FROM rating_curves")
    filenames += [row[0] for row in cursor.fetchall()]

    flood_db = os.path.splitext(db_file)[0]
    json_filename = flood_db + ".json"
    vis_json_filename = flood_db + "_vis.json"
    filenames += [db_file, json_filename, vis_json_filename, "README.txt"]
    filenames = list(set(f for f in filenames if str(f) != "nan"))

    filenames_df = pd.DataFrame(filenames, columns=["filename"])
    filenames_df = filenames_df.sort_values(by="filename")
    os.makedirs(output_dir, exist_ok=True)
    filenames_df.to_csv(os.path.join(output_dir, "filenames.csv"), index=False)

    with open(os.path.join(output_dir, "README.txt"), "w") as f:
        f.write(
            f"The database file is the {db_file} file, and it is also "
            f"available as a JSON file as {json_filename}.\n\n"
            f"The file {vis_json_filename} is used to load the database "
            f"as a scenario into the visualization tool.\n\n"
            "The rating curves are the CSV files in the resource, and the "
            "individual rasters and shapefiles are the extent, depth, and "
            "water surface elevation files.\n\n"
            "The file naming system is set up so that it is "
            "FIMSourceID-RatingCurveID-type-flow.\n\n"
            "Type is Extent (EXT), Depth (DEP), Water Surface Elevation "
            "(WSE), Velocity (VEL), or Boundary (BND)."
        )

    # Process dataframe for renaming
    file_id_dict = {}
    unique_fnames = filenames_df["filename"].unique()
    query = (
        f"SELECT FileName, FileId FROM files "
        f"WHERE FileName IN ({','.join('?' * len(unique_fnames))})"
    )
    cursor.execute(query, tuple(unique_fnames))
    for row in cursor.fetchall():
        file_id_dict[row[0]] = row[1]

    filenames_df["FileID"] = filenames_df["filename"].map(file_id_dict)

    flow_queries = [
        "SELECT * FROM flows WHERE FloodExtentVectorID = ?",
        "SELECT * FROM flows WHERE DepthRasterID = ?",
        "SELECT * FROM flows WHERE WSERasterID = ?",
        "SELECT * FROM flows WHERE VelocityRasterID = ?",
        "SELECT * FROM flows WHERE BoundaryVectorID = ?",
    ]

    flow_data_dict = {}
    for file_id in filenames_df["FileID"].dropna().unique():
        for q in flow_queries:
            cursor.execute(q, (file_id,))
            result = cursor.fetchone()
            if result:
                flow_data_dict[file_id] = result
                break

    if flow_data_dict:
        flow_df = pd.DataFrame.from_dict(
            flow_data_dict,
            orient="index",
            columns=[
                "RatingCurveID", "flow", "depth", "rp",
                "downstream_wse",
                "FloodExtentVectorID", "DepthRasterID",
                "WSERasterID", "VelocityRasterID", "BoundaryVectorID",
            ],
        )
        joined_df = filenames_df.merge(
            flow_df, how="left", left_on="FileID", right_index=True
        )
    else:
        joined_df = filenames_df.copy()

    rc_exist = pd.read_sql_query(
        "SELECT RatingCurveID, RatingCurve FROM rating_curves "
        "ORDER BY RatingCurveID",
        conn,
    )
    joined_df = joined_df.merge(rc_exist, how="left", on="RatingCurveID")

    fifs_exist = pd.read_sql_query(
        "SELECT * FROM feature_ids_fim_source ORDER BY RatingCurveID", conn
    )
    full_data = joined_df.merge(
        fifs_exist, how="inner", on="RatingCurveID"
    )
    unique_full_data = full_data.drop_duplicates(subset="FileID", keep="first")
    fim_source_df = unique_full_data[["RatingCurveID", "FimSourceID"]]

    joined_df["FIMSourceID"] = joined_df.apply(
        lambda row: (
            fim_source_df.loc[
                fim_source_df["RatingCurveID"] == row["RatingCurveID"],
                "FimSourceID",
            ].values[0]
            if row["RatingCurveID"] in fim_source_df["RatingCurveID"].values
            else None
        ),
        axis=1,
    )
    joined_df = joined_df.sort_values(by=["FileID"])

    def create_filename(row):
        fim_source_id = (
            int(row["FIMSourceID"]) if pd.notna(row.get("FIMSourceID")) else None
        )
        file_id = (
            int(row["FileID"]) if pd.notna(row.get("FileID")) else None
        )
        rc_id = (
            int(row["RatingCurveID"]) if pd.notna(row.get("RatingCurveID")) else None
        )
        ext_id = (
            int(row["FloodExtentVectorID"])
            if pd.notna(row.get("FloodExtentVectorID"))
            else None
        )
        dep_id = (
            int(row["DepthRasterID"])
            if pd.notna(row.get("DepthRasterID"))
            else None
        )
        wse_id = (
            int(row["WSERasterID"])
            if pd.notna(row.get("WSERasterID"))
            else None
        )
        vel_id = (
            int(row["VelocityRasterID"])
            if pd.notna(row.get("VelocityRasterID"))
            else None
        )
        bnd_id = (
            int(row["BoundaryVectorID"])
            if pd.notna(row.get("BoundaryVectorID"))
            else None
        )
        flow = int(row["flow"]) if pd.notna(row.get("flow")) else None
        downstream_wse_val = (
            float(row["downstream_wse"])
            if pd.notna(row.get("downstream_wse"))
            else None
        )
        filename = row["filename"]

        if pd.isna(file_id):
            return filename

        parts = [str(fim_source_id), str(rc_id)]
        if ext_id == file_id:
            parts.append("EXT")
        elif dep_id == file_id:
            parts.append("DEP")
        elif wse_id == file_id:
            parts.append("WSE")
        elif vel_id == file_id:
            parts.append("VEL")
        elif bnd_id == file_id:
            parts.append("BND")
        parts.append(str(flow))
        # Downstream BC is encoded by RatingCurveID, so no WSE token is
        # appended here — keeps names uniform: FIMSourceID-RatingCurveID-TYPE-flow.

        new_name = "-".join(parts)
        ext = filename[filename.rfind("."):]
        return new_name + ext

    joined_df["new_filename"] = joined_df.apply(create_filename, axis=1)
    processed = joined_df[["filename", "new_filename"]].dropna()

    # Add shapefile auxiliary files
    shp_rows = processed[
        processed["new_filename"].notna()
        & processed["new_filename"].str.endswith(".shp")
    ]
    aux_dfs = []
    for ext in ["dbf", "prj", "shx", "cpg"]:
        aux_df = shp_rows.apply(
            lambda row: pd.Series({
                "filename": row["filename"].replace(".shp", f".{ext}"),
                "new_filename": row["new_filename"].replace(".shp", f".{ext}"),
            }),
            axis=1,
        )
        aux_dfs.append(aux_df)

    processed = pd.concat([processed] + aux_dfs, ignore_index=True)
    processed.to_csv(os.path.join(output_dir, "new_filenames.csv"), index=False)

    # Check files exist (skip JSON files that haven't been created yet)
    skip_suffixes = (".json",)
    bad_files = []
    for fname in processed["filename"]:
        if fname.endswith(skip_suffixes):
            continue
        full_path = os.path.join(gis_dir, fname) if gis_dir else fname
        if not os.path.exists(full_path) and not os.path.exists(fname):
            bad_files.append(fname)
    if bad_files:
        print(f"Warning: {len(bad_files)} file(s) not found:")
        for f in bad_files[:10]:
            print(f"  {f}")

    # Rename files (skip non-GIS files like .db, .json, .csv, .txt)
    skip_extensions = (".db", ".json", ".csv", ".txt")
    renamed = 0
    for _, row in processed.iterrows():
        old_name = row["filename"]
        new_name = row["new_filename"]
        if old_name.endswith(skip_extensions):
            continue
        old_path = (
            os.path.join(gis_dir, old_name)
            if gis_dir and os.path.exists(os.path.join(gis_dir, old_name))
            else old_name
        )
        if os.path.exists(old_path):
            new_path = (
                os.path.join(gis_dir, new_name) if gis_dir else new_name
            )
            os.rename(old_path, new_path)
            renamed += 1

    # Update filenames in database
    update_query = "UPDATE files SET filename = ? WHERE filename = ?"
    for _, row in processed.iterrows():
        cursor.execute(update_query, (row["new_filename"], row["filename"]))
    conn.commit()

    print(f"Renamed {renamed} files. Mapping saved to new_filenames.csv")
    return processed


# ─── Step 6: Generate JSON files ────────────────────────────────────────────

def generate_json_files(conn, db_file, hydroshare_cfg, resource_id="",
                        output_dir=".", base_url=None):
    flood_db = os.path.splitext(os.path.basename(db_file))[0]

    # Simple DB-to-JSON export
    json_filename = os.path.join(output_dir, flood_db + ".json")
    data = {}
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row["name"] for row in cursor.fetchall()]

    for table in tables:
        cursor.execute(f"SELECT * FROM {table}")
        rows = cursor.fetchall()
        data[table] = [dict(row) for row in rows]

    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"Created {json_filename}")

    # Reset row_factory
    conn.row_factory = None
    cursor = conn.cursor()

    # Visualization JSON
    vis_json_filename = os.path.join(output_dir, flood_db + "_vis.json")
    hs = hydroshare_cfg
    vis_json = {
        "location": flood_db,
        "details": {
            "date_created": hs.get("date_created", ""),
            "contact_name": hs.get("contact_name", ""),
            "contact_email": hs.get("contact_email", ""),
            "stage_unit": hs.get("stage_unit", "ft"),
            "discharge_unit": hs.get("discharge_unit", "cfs"),
            "terms_of_use": "provisional - not for regulatory use",
            "depth_service_url": "",
        },
        "gauge_info": {
            "river": hs.get("river_name", ""),
            "gauge_id": hs.get("gauge_id", ""),
            "latitude": hs.get("gauge_latitude", 0),
            "longitude": hs.get("gauge_longitude", 0),
            "flood_level": hs.get("flood_level", ""),
            "last_reported_value": "",
            "last_reported_time": "",
        },
        "scenarios": [],
        "time_series": [],
    }

    # Resolve the base URL that fronts every map file. Custom base_url wins
    # (local server / GitHub raw / S3 bucket); otherwise fall back to a
    # HydroShare resource URL built from resource_id.
    if base_url:
        file_base = base_url.rstrip("/")
    else:
        if not resource_id:
            resource_id = hs.get("resource_id", "RESOURCE_ID_HERE")
        file_base = (
            f"https://www.hydroshare.org/resource/{resource_id}/data/contents"
        )

    stage_unit = hs.get("stage_unit", "ft")

    # One scenarios[] entry per rating curve = (model x downstream BC).
    cursor.execute("""
        SELECT rc.RatingCurveID, rc.DownstreamBCType, rc.DownstreamBCValue,
               fs.FIMSourceName, f.Flow, f.Depth, f.ReturnPeriod,
               f.DownstreamWSE, ext.FileName AS extent_file,
               dep.FileName AS depth_file
        FROM rating_curves rc
        INNER JOIN (
            SELECT RatingCurveID, MIN(FimSourceID) AS FirstFimSourceID
            FROM feature_ids_fim_source
            GROUP BY RatingCurveID
        ) fifs ON rc.RatingCurveID = fifs.RatingCurveID
        INNER JOIN fim_source fs ON fifs.FirstFimSourceID = fs.FIMSourceID
        INNER JOIN flows f ON rc.RatingCurveID = f.RatingCurveID
        LEFT JOIN files ext ON f.FloodExtentVectorID = ext.FileID
        LEFT JOIN files dep ON f.DepthRasterID = dep.FileID
        ORDER BY rc.RatingCurveID, f.Flow
    """)

    scenario_map = {}   # RatingCurveID -> scenario dict
    time_series_entries = []

    for row in cursor.fetchall():
        (rc_id, bc_type, bc_value, fim_source_name, flow, depth,
         return_period, downstream_wse, extent_file, depth_file) = row

        if rc_id not in scenario_map:
            scenario_map[rc_id] = {
                "model": bc_display_label(
                    fim_source_name.strip(), bc_type, bc_value, stage_unit),
                "scenario_list": [],
            }

        item = {
            "vector_file": f"{file_base}/{extent_file}" if extent_file else "",
            "raster_file": f"{file_base}/{depth_file}" if depth_file else "",
            "stage": depth,
            "discharge": flow,
            "downstream_wse": downstream_wse,
            "annual_chance": (
                f"{int(return_period)} year" if return_period else ""
            ),
            "layer_id": len(scenario_map[rc_id]["scenario_list"]),
        }
        scenario_map[rc_id]["scenario_list"].append(item)
        time_series_entries.append((flow, depth))

    time_series_entries = sorted(time_series_entries, key=lambda x: x[0])

    for i, (flow, depth) in enumerate(time_series_entries[:8]):
        vis_json["time_series"].append({
            "stage": depth,
            "discharge": flow,
            "timestamp": f"2025-07-04 {10 + i:02d}:00:00",
            "type": "o" if i < 5 else "f",
        })

    vis_json["scenarios"] = list(scenario_map.values())

    with open(vis_json_filename, "w") as f:
        json.dump(vis_json, f, indent=4)
    print(f"Created {vis_json_filename}")


# ─── Step 7: Upload to HydroShare ───────────────────────────────────────────

def upload_to_hydroshare(conn, db_file, hydroshare_cfg, new_filenames, gis_dir="", output_dir="."):
    try:
        from hsclient import HydroShare
    except ImportError:
        print(
            "hsclient not installed. Install with: pip install hsclient"
        )
        print("Skipping HydroShare upload.")
        return

    hs = HydroShare()
    hs.sign_in()

    resource_type = hydroshare_cfg.get("resource_type", "new")
    resource_id = hydroshare_cfg.get("resource_id", "")

    if resource_type == "new":
        new_resource = hs.create()
        resource_id = new_resource.resource_id
        print(f"New resource: {new_resource.metadata.url}")
        new_resource.metadata.title = hydroshare_cfg.get(
            "resource_name", "FIM Database"
        )
        new_resource.metadata.abstract = hydroshare_cfg.get(
            "resource_description", ""
        )
        subjects = hydroshare_cfg.get("resource_subjects", [])
        if subjects:
            new_resource.metadata.subjects = subjects
        new_resource.save()
        resource = new_resource
    else:
        resource = hs.resource(resource_id)

    # Now that we have the resource_id, regenerate JSON files with correct URLs
    print("  Generating JSON files with resource URL...")
    generate_json_files(conn, db_file, hydroshare_cfg, resource_id, output_dir)

    flood_db = os.path.splitext(os.path.basename(db_file))[0]
    json_filename = os.path.join(output_dir, flood_db + ".json")
    vis_json_filename = os.path.join(output_dir, flood_db + "_vis.json")

    for f in resource.files():
        if f.endswith(os.path.basename(json_filename)) or f.endswith(os.path.basename(vis_json_filename)):
            resource.file_delete(f)
            print(f"Deleted old file: {f}")

    resource.file_upload(json_filename)
    resource.file_upload(vis_json_filename)

    # Build full paths for GIS files, skip non-GIS files
    skip_extensions = (".db", ".json", ".csv", ".txt")
    file_list = []
    for fname in new_filenames["new_filename"].tolist():
        if fname.endswith(skip_extensions):
            continue
        full_path = os.path.join(gis_dir, fname) if gis_dir else fname
        if os.path.exists(full_path):
            file_list.append(full_path)
        elif os.path.exists(fname):
            file_list.append(fname)

    # Upload DB file, rating curve CSVs, and README
    readme_path = os.path.join(output_dir, "README.txt")
    for extra in [db_file, readme_path]:
        if os.path.exists(extra):
            file_list.append(extra)
    for fname in new_filenames["new_filename"].tolist():
        if fname.endswith(".csv") and os.path.exists(fname):
            file_list.append(fname)

    uploaded = 0
    failed = []
    for f in file_list:
        try:
            resource.file_upload(f)
            uploaded += 1
            print(f"  Uploaded: {os.path.basename(f)}")
        except Exception as e:
            failed.append(os.path.basename(f))
            print(f"  Failed: {os.path.basename(f)} ({e})")

    print(f"\nUploaded {uploaded}/{len(file_list)} files to HydroShare")
    if failed:
        print(
            f"  {len(failed)} file(s) failed but may still have uploaded "
            "server-side. Check your resource on HydroShare."
        )


# ─── Step 6.5: Assemble a single ready-to-push publish folder ───────────────

def assemble_publish_folder(gis_dir, extra_files, publish_dir, reach_id):
    """Copy renamed map files + vis JSON into publish_dir/<reach_id>/ so the
    whole set can be dragged/pushed to the GitHub host in one folder."""
    dest = os.path.join(publish_dir, str(reach_id))
    os.makedirs(dest, exist_ok=True)

    n = 0
    for fp in glob.glob(os.path.join(gis_dir, "*")):
        if os.path.isfile(fp):
            shutil.copy2(fp, os.path.join(dest, os.path.basename(fp)))
            n += 1
    for fp in extra_files:
        if fp and os.path.exists(fp):
            shutil.copy2(fp, os.path.join(dest, os.path.basename(fp)))
            n += 1

    print(f"Assembled {n} files into publish folder: {dest}")
    return dest


# ─── Main ────────────────────────────────────────────────────────────────────

def load_config(config_path):
    with open(config_path) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(
        description="Populate FIM database"
    )
    parser.add_argument(
        "--config", required=True, help="Path to YAML config file"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    db_file = cfg["database"]["filename"]
    db_type = cfg["database"].get("type", "new")
    processed_dir = cfg["processed_dir"]
    fim_source_csv = os.path.join(processed_dir, "FIM_input_data.csv")
    huc8_csv = os.path.join(processed_dir, "HUC8.csv")
    feature_ids_file = os.path.join(processed_dir, "feature_IDs.csv")
    gis_dir = os.path.join(processed_dir, "final_gis_files")
    hydroshare_cfg = cfg.get("hydroshare", {})
    do_upload = cfg.get("upload_to_hydroshare", False)
    output_dir = os.path.dirname(db_file) or "."

    defaults = cfg.get("model_defaults", {})

    # Rating-curve manifest (one row per model x downstream BC) is the
    # contract from run_preprocessing.py.
    manifest_path = os.path.join(processed_dir, "rating_curve_manifest.csv")
    if not os.path.exists(manifest_path):
        raise SystemExit(
            f"Missing {manifest_path}. Run run_preprocessing.py first."
        )
    manifest = pd.read_csv(manifest_path)
    print(f"Manifest: {len(manifest)} rating curve(s) across "
          f"{manifest['model'].nunique()} model(s): "
          f"{sorted(manifest['model'].unique())}")

    # Base URL that fronts the map files in the vis JSON.
    #   custom     -> use cfg['base_url'] verbatim (local server / GitHub / S3)
    #   hydroshare -> built later from the uploaded resource_id
    base_url_mode = cfg.get("base_url_mode", "custom")
    base_url = cfg.get("base_url") if base_url_mode == "custom" else None

    # Auto-populate hydroshare/gauge metadata from processed data
    if hydroshare_cfg:
        feature_ids_df = pd.read_csv(feature_ids_file)
        downstream = feature_ids_df.loc[feature_ids_df["latitude"].idxmin()]
        hydroshare_cfg.setdefault("gauge_id", f"NWM-{int(downstream['feature_id'])}")
        hydroshare_cfg.setdefault("gauge_latitude", round(float(downstream["latitude"]), 3))
        hydroshare_cfg.setdefault("gauge_longitude", round(float(downstream["longitude"]), 3))
        hydroshare_cfg.setdefault("date_created", datetime.now().strftime("%B %Y"))
        hydroshare_cfg.setdefault("stage_unit", defaults.get("depth_unit", "ft"))
        hydroshare_cfg.setdefault("discharge_unit", defaults.get("flow_unit", "cfs"))

    # Step 1: Create/connect database
    print("\n>>> Step 1: Setting up database...")
    os.makedirs(output_dir, exist_ok=True)
    conn = create_database(db_file, db_type)

    # Step 2: One fim_source per model
    print("\n>>> Step 2: Populating FIM source table...")
    populate_fim_source(conn, fim_source_csv)
    fs_map = {
        row["FIMSourceName"]: int(row["FIMSourceID"])
        for _, row in pd.read_sql_query(
            "SELECT FIMSourceName, FIMSourceID FROM fim_source", conn
        ).iterrows()
    }

    # Steps 3 & 4: per model -> HUC8, then a rating curve per downstream BC
    for model in manifest["model"].unique():
        fim_source_id = fs_map[model]
        print(f"\n>>> Model {model} (FIMSourceID={fim_source_id})")
        add_huc8_data(conn, huc8_csv, fim_source_id)

        for r in manifest[manifest["model"] == model].itertuples(index=False):
            model_cfg = {
                "rating_curve_file": os.path.join(processed_dir, r.csv_file),
                "feature_ids_file": feature_ids_file,
                "primary_rating_curve": bool(int(r.is_primary)),
                "flow_unit": defaults.get("flow_unit", "cfs"),
                "depth_unit": defaults.get("depth_unit", "ft"),
                "nwm_reference_version": defaults.get("nwm_reference_version", "3.0"),
                "dsbc_type": r.dsbc_type,
                "dsbc_value": r.dsbc_value,
            }
            print(f"  + rating curve {r.csv_file} (BC={r.dsbc_type})")
            add_rating_curve(conn, model_cfg, fim_source_id)

    # Step 5: Generate filenames and rename
    print("\n>>> Step 5: Generating filenames and renaming files...")
    processed = generate_filenames_and_rename(conn, db_file, gis_dir, output_dir)

    # Step 6: Generate JSON files
    print("\n>>> Step 6: Generating JSON files...")
    generate_json_files(conn, db_file, hydroshare_cfg, output_dir=output_dir,
                        base_url=base_url)

    # Step 6.5: Assemble one ready-to-push publish folder
    print("\n>>> Step 6.5: Assembling publish folder...")
    flood_db = os.path.splitext(os.path.basename(db_file))[0]
    vis_json = os.path.join(output_dir, flood_db + "_vis.json")
    reach_id = cfg.get("reach_id", "reach")
    publish_dir = cfg.get("publish_dir", "../All_Outputs/Publish")
    publish_path = assemble_publish_folder(gis_dir, [vis_json], publish_dir,
                                           reach_id)
    if base_url:
        print(f"    Push {publish_path}/ to your host, then open:\n"
              f"    {base_url.rstrip('/')}/{flood_db}_vis.json")

    # Step 7: Upload to HydroShare (optional)
    if do_upload:
        print("\n>>> Step 7: Uploading to HydroShare...")
        upload_to_hydroshare(conn, db_file, hydroshare_cfg, processed, gis_dir, output_dir)
    else:
        print("\n>>> Step 7: Skipping HydroShare upload (disabled in config)")

    conn.close()
    print(f"\nDone. Database saved as: {db_file}")


if __name__ == "__main__":
    main()
