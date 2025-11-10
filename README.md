# Multi-model_FIM_integration_database

This repository provides Google Colab notebooks for integrating and populating a **multi-model Flood Inundation Mapping (FIM) database** connected to the **National Water Model (NWM)**.  
The workflow harmonizes flood maps from various models — including FEMA, InFRM, USGS, and HAND — into a consistent relational structure for visualization and comparison.

---

## Overview

This collection of notebooks supports the full workflow for:
- Acquiring flood maps from multiple model sources (FEMA, InFRM, USGS, etc.)  
- Generating baseline NWM HAND-based FIMs using the **FIM-Serv** framework  
- Standardizing, preprocessing, and loading FIM datasets into a harmonized database  

All notebooks are designed for step-by-step execution within Google Colab.

---

## Notebooks

| Notebook | Description |
|-----------|--------------|
| **1_FEMA.ipynb** | Processes FEMA flood maps for use in the database. |
| **2_InFRM_USGS_renaming.ipynb** | Standardizes filenames from InFRM or USGS using rating-curve-based naming conventions. |
| **3_FIMServe_BYU_version.ipynb** | Generates NWM HAND-based flood maps using the FIM-Serv workflow. |
| **4_FIM_database_preprocessing.ipynb** | Prepares raster, vector, and metadata files before loading. |
| **5_PopulateFIM_to_database.ipynb** | Populates the harmonized database with processed maps and metadata. |

---

## Workflow Explanation

Processes **1** and **2** correspond to importing and preparing flood maps from **FEMA**, **InFRM**, and **USGS** sources.  
These steps are optional and can be adapted for any other additional flood map sources.

Process **3** is **mandatory**, as it generates the **National Water Model (NWM) HAND-based FIM** using the FIM-Serv framework.  
Processes **4** and **5** must be run **after** process 3, to perform preprocessing and database population.

Together, these steps integrate multiple FIM datasets into a unified database for comparison and visualization.

---

## Figures

### 1. FIM Database Relational Diagram
This diagram shows the relational structure of the multi-model FIM database, including tables for model types, rating curves, and flood extents.

![Database Relational Diagram](Database_Relational_Diagram.jpg)

### 2. FIM Database Architecture
This figure illustrates how flood maps from FEMA, InFRM, USGS, and the NWM HAND model are integrated through the database for on-demand visualization.

![FIM Architecture](FIM_architecture_diagram.jpg)

---

## FIM-Serv Framework

This project uses the **OWP HAND-FIM “as a service” (FIMserv)** framework for generating NWM HAND flood maps.  
The framework is maintained under the [NOAA OWP HAND-FIM repository](https://github.com/NOAA-OWP/inundation-mapping) and developed by the **Surface Dynamics Modeling Lab (SDML)** at **Brigham Young University**, supported by the **Cooperative Institute for Research to Operations in Hydrology (CIROH)**.

---

## License

Released under the **BSD 3-Clause License**.

---

## Citation

If you use this repository, please cite:

> Wagle, P., Nelson, E., et al. (2025). *Multi-model FIM Integration Database.* GitHub Repository.
