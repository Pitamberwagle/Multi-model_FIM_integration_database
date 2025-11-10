# Multi-model_FIM_integration_database

This repository provides Google Colab notebooks for integrating and populating a **multi-model Flood Inundation Mapping (FIM) database** connected to the **National Water Model (NWM)**.  
The workflow harmonizes flood maps from multiple models — including FEMA, InFRM, USGS, and HAND — into a consistent relational structure for visualization and comparison.

---

## Overview

This collection of notebooks supports the full workflow for:
- Acquiring and processing flood maps from multiple model sources (FEMA, InFRM, USGS, and others)  
- Generating baseline NWM HAND-based FIMs using the **FIM-Serv** framework  
- Standardizing, preprocessing, and populating a harmonized multi-model database for visualization and comparison  

### 1. FIM Database Relational Diagram
This diagram shows the relational structure of the multi-model FIM database, including tables for model types, rating curves, and flood extents.

![Database Relational Diagram](Database_Relational_Diagram.jpg)
All notebooks are designed for **step-by-step execution** within **Google Colab**. Each notebook includes inline instructions and user input prompts for clarity.

### 2. FIM Database Architecture
This figure illustrates how flood maps from FEMA, InFRM, USGS, and the NWM HAND model are integrated through the database for on-demand visualization.

![FIM Database Architecture](FIM_architecture_diagram.jpg)

---

## Notebooks

All notebooks are stored in the `notebooks/` folder.

| Notebook | Description |
|-----------|--------------|
| **1_FEMA.ipynb** | Processes FEMA flood maps for use in the database. |
| **2_InFRM_USGS_renaming.ipynb** | Standardizes filenames from InFRM or USGS using rating-curve-based naming conventions. |
| **3_FIMServe_BYU_version.ipynb** | Generates NWM HAND-based flood maps using the FIM-Serv framework. |
| **4_FIM_database_preprocessing.ipynb** | Prepares raster, vector, and metadata files before loading. |
| **5_PopulateFIM_to_database.ipynb** | Populates the harmonized database with processed maps and metadata. |

---

## Workflow Explanation

Processes **1** and **2** correspond to importing and preparing flood maps from **FEMA**, **InFRM**, and **USGS** sources.  
These steps are **optional** and can be adapted for additional flood map sources that follow similar data structures.

Process **3** is **mandatory**, as it generates the baseline **National Water Model (NWM) HAND-based FIM** using the FIM-Serv framework.  
Processes **4** and **5** follow sequentially after process 3, performing preprocessing and database population.

Together, these steps integrate multiple FIM datasets into a harmonized database for visualization and evaluation.
---

## FIM-Serv Framework

This project uses the **OWP HAND-FIM “as a service” (FIMserv)** framework for generating the baseline **National Water Model HAND-based FIMs**.  
FIMserv provides a streamlined, cloud-enabled pipeline for producing inundation extent and depth rasters using retrospective or forecasted NWM streamflow data.

FIMserv is developed under the **Surface Dynamics Modeling Lab (SDML)** at the **University of Alabama**, supported by the  
**Cooperative Institute for Research to Operations in Hydrology (CIROH)**.

📘 GitHub Repository: [https://github.com/sdmlua/FIMserv](https://github.com/sdmlua/FIMserv)

---

## License

This repository is released under the **BSD 3-Clause License**.

---

## Citation

If you use this repository, please cite:

> Wagle, P., Nelson, E., et al. (2025). *Multi-model FIM Integration Database.* GitHub Repository.

---

