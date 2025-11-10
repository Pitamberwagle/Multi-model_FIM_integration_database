# Multi-model_FIM_integration_database

This repository contains Google Colab notebooks for integrating flood inundation maps (FIMs) from multiple sources into a harmonized database linked to the National Water Model (NWM).  

The notebooks can be used to preprocess, rename, and populate the database following a consistent relational database structure structure.

---

## 📁 Notebooks

All notebooks are stored in the `notebooks/` folder.

| Notebook | Description |
|-----------|--------------|
| **1_FEMA.ipynb** | Processes FEMA flood maps for use in the database. Use the code after you download FEMA map from NFHL |
| **2_InFRM_USGS_renaming.ipynb** | Standardizes filenames using rating-curve-based naming conventions. Use the code after you download the InFRM and/or USGS maps |
| **3_FIMServe_BYU_version.ipynb** | Generates baseline HAND/NWM flood maps using the FIM-Serv workflow. |
| **4_FIM_database_preprocessing.ipynb** | Prepares rasters, vectors, and metadata before loading. |
| **5_PopulateFIM_to_database.ipynb** | Populates the harmonized database with processed data. |

---

---

### 🔄 Workflow Explanation

Processes **1** and **2** correspond to importing and standardizing flood maps from external sources such as **FEMA**, **InFRM**, and **USGS**.  
These steps can also be applied to any additional flood map sources following similar data structures.

Process **3** is the mandatory step — it generates the baseline **National Water Model (NWM) HAND-based FIM** using the FIM-Serv workflow.  
Processes **4** and **5** then follow sequentially, performing database preprocessing and population.

Together, these steps enable integration of multiple FIM datasets into a harmonized database for visualization and comparison.

## 🧭 How to Use

1. Open the notebooks in order using **Google Colab**.  
2. Follow the step-by-step instructions in each notebook.  
3. Provide file paths and user inputs as prompted.  
4. Save outputs (rasters, shapefiles, or database files) locally.

---

## 🖼 Database Diagram

The database follows the structure shown below:

![Database Diagram](Database_Relational_Diagram.jpg)

---

## 📜 License

This repository is released under the **BSD 3-Clause License**.

---

## 📘 Reference

If you use this work, please cite:

> Wagle, P. and Nelson, E. (2025). *Multi-model FIM Integration Database.* GitHub Repository.
