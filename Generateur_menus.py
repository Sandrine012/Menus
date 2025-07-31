import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta

# NOUVEAU: Importation des classes et constantes de generation.py
from generation import (
    RecetteManager, MenusHistoryManager, MenuGenerator,
    COLONNE_NOM, COLONNE_ID_RECETTE, COLONNE_ID_INGREDIENT, COLONNE_AIME_PAS_PRINCIP,
    VALEUR_DEFAUT_TEMPS_PREPARATION, TEMPS_MAX_EXPRESS, TEMPS_MAX_RAPIDE, REPAS_EQUILIBRE,
    NB_JOURS_ANTI_REPETITION, FICHIER_RECETTES, FICHIER_PLANNING, FICHIER_MENUS,
    FICHIER_INGREDIENTS, FICHIER_INGREDIENTS_RECETTES, FICHIER_SORTIE_MENU_CSV,
    FICHIER_SORTIE_LISTES_TXT
)

# Configuration du logger pour Streamlit
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# NOTE: Les constantes globales et les classes RecetteManager, MenusHistoryManager, MenuGenerator
# qui étaient définies directement dans Generateur_menus.py ont été supprimées d'ici
# car elles sont maintenant importées de generation.py. Ceci évite les doublons.

def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(colonnes_manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}")

def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus Automatique")
    st.title("🍽️ Générateur de Menus")

    st.sidebar.header("Paramètres")

    uploaded_files = {}
    file_configs = {
        "Recettes.csv": {"key": "recettes_file", "label": "Uploader Recettes.csv", "required_cols": ["Page_ID", "Nom", "Temps_total", "Aime_pas_princip", "Transportable", "Calories", "Proteines"]},
        "Planning.csv": {"key": "planning_file", "label": "Uploader Planning.csv", "required_cols": ["Date", "Participants", "Transportable", "Temps", "Nutrition"]},
        "Ingredients.csv": {"key": "ingredients_file", "label": "Uploader Ingredients.csv", "required_cols": ["Page_ID", "Nom", "Qte reste", "unité"]},
        "Ingredients_recettes.csv": {"key": "ingredients_recettes_file", "label": "Uploader Ingredients_recettes.csv", "required_cols": ["Page_ID", "Ingrédient ok", "Qté/pers_s"]},
        "Menus.csv": {"key": "menus_file", "label": "Uploader Menus.csv (Historique)", "required_cols": ["Date", "Recette"]}
    }

    for file_name, config in file_configs.items():
        uploaded_file = st.sidebar.file_uploader(config["label"], type="csv", key=config["key"])
        if uploaded_file is not None:
            try:
                # Tentative de lire avec le délimiteur par défaut (virgule), puis avec point-virgule
                df = pd.read_csv(uploaded_file, encoding='utf-8')
                if len(df.columns) == 1 and ';' in df.iloc[0, 0]: # Vérifier si c'est un CSV avec ';' comme séparateur
                    uploaded_file.seek(0) # Remettre le pointeur au début du fichier
                    df = pd.read_csv(uploaded_file, encoding='utf-8', sep=';')
                
                # Assurer que les colonnes sont du bon type si nécessaire, par exemple pour "Temps_total"
                if "Temps_total" in df.columns:
                    df["Temps_total"] = pd.to_numeric(df["Temps_total"], errors='coerce').fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)
                if "Calories" in df.columns:
                    df["Calories"] = pd.to_numeric(df["Calories"], errors='coerce') # Garder en float pour comparaison
                if "Proteines" in df.columns: # Ajouté pour s'assurer que Protéines est numérique
                    df["Proteines"] = pd.to_numeric(df["Proteines"], errors='coerce')

                verifier_colonnes(df, config["required_cols"], file_name)
                uploaded_files[file_name] = df
            except Exception as e:
                st.error(f"Erreur lors du chargement ou de la vérification de {file_name}: {e}")
                logger.exception(f"Erreur de chargement pour {file_name}")

    all_files_uploaded = all(name in uploaded_files for name in file_configs.keys())

    if st.sidebar.button("Générer le Menu", disabled=not all_files_uploaded):
        if not all_files_uploaded:
            st.warning("Veuillez uploader tous les fichiers CSV nécessaires pour générer le menu.")
            return

        try:
            # Assurez-vous que les DataFrames sont passés dans le bon ordre ou par nom
            dataframes = {
                "Menus": uploaded_files["Menus.csv"],
                "Recettes": uploaded_files["Recettes.csv"],
                "Planning": uploaded_files["Planning.csv"],
                "Ingredients": uploaded_files["Ingredients.csv"],
                "Ingredients_recettes": uploaded_files["Ingredients_recettes.csv"]
            }

            # L'initialisation de MenuGenerator utilise les DataFrames
            menu_generator = MenuGenerator(
                dataframes["Menus"],
                dataframes["Recettes"],
                dataframes["Planning"],
                dataframes["Ingredients"],
                dataframes["Ingredients_recettes"]
            )
            df_menu_genere, liste_courses = menu_generator.generer_menu()

            st.success("🎉 Menu généré avec succès !")

            st.header("2. Menu Généré")
            st.dataframe(df_menu_genere)

            st.header("3. Liste de Courses (Ingrédients manquants cumulés)")
            if liste_courses:
                liste_courses_df = pd.DataFrame(liste_courses.items(), columns=["Ingrédient", "Quantité manquante"])
                st.dataframe(liste_courses_df)

                # Option de téléchargement de la liste de courses
                csv = liste_courses_df.to_csv(index=False, sep=';', encoding='utf-8-sig') # utf-8-sig pour Excel
                st.download_button(
                    label="Télécharger la liste de courses (CSV)",
                    data=csv,
                    file_name="liste_courses.csv",
                    mime="text/csv",
                )
            else:
                st.info("Aucun ingrédient manquant identifié pour la liste de courses.")

        except ValueError as ve:
            st.error(f"Erreur de données: {ve}")
            logger.exception("Erreur de données lors de la génération du menu")
        except Exception as e:
            st.error(f"Une erreur inattendue est survenue lors de la génération: {e}")
            logger.exception("Erreur inattendue lors de la génération du menu dans Streamlit")

if __name__ == "__main__":
    main()
