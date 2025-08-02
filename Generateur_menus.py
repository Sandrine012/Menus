import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta

# ------------------------------------------------------------------
#                         CONSTANTES GLOBALES
# ------------------------------------------------------------------
NB_JOURS_ANTI_REPETITION = 42
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"          # utilisé comme ID pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID"       # utilisé comme ID pour Ingredients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"
VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700

# ------------------------------------------------------------------
#                       CONFIGURATION LOGGER
# ------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
#                          OUTILS COMMUNS
# ------------------------------------------------------------------
def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    """Vérifie si toutes les colonnes attendues sont présentes dans le DataFrame."""
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(
            f"Colonnes manquantes dans {nom_fichier}: {', '.join(colonnes_manquantes)}"
        )
        raise ValueError(
            f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}"
        )

# ------------------------------------------------------------------
#                        CLASSES MÉTIER (inchangées)
# ------------------------------------------------------------------
# ... (toutes les classes RecetteManager, MenusHistoryManager, MenuGenerator
#      sont reprises sans aucune modification fonctionnelle)
# ------------------------------------------------------------------
# Pour éviter un message trop long, ces classes sont omises ici mais
# doivent être copiées **à l’identique** depuis votre code d’origine.
# ------------------------------------------------------------------

# ------------------------------------------------------------------
#                        INTERFACE STREAMLIT
# ------------------------------------------------------------------
def main():
    st.set_page_config(
        layout="wide", page_title="Générateur de Menus et Liste de Courses"
    )
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Veuillez charger tous les fichiers CSV nécessaires.")

    # ----------- 1. Uploader combiné Recettes + Ingredients_recettes -----------
    uploaded_recettes_combo = st.sidebar.file_uploader(
        "Uploader Recettes.csv ET Ingredients_recettes.csv (sélectionnez les deux)",
        type="csv",
        accept_multiple_files=True,
        key="recettes_combo",
    )

    # -------------------- 2. Uploader Ingredients --------------------
    uploaded_ingredients = st.sidebar.file_uploader(
        "Uploader Ingredients.csv",
        type="csv",
        key="ingredients_file",
    )

    # ----------- 3. Uploader combiné Planning + Menus ----------------
    uploaded_planning_menus = st.sidebar.file_uploader(
        "Uploader Planning.csv ET Menus.csv (sélectionnez les deux)",
        type="csv",
        accept_multiple_files=True,
        key="planning_menus_combo",
    )

    # ----------------------------------------------------------------
    #                  LECTURE DES FICHIERS & VALIDATION
    # ----------------------------------------------------------------
    dataframes = {}
    all_files_uploaded = True

    # --- traitement Recettes + Ingredients_recettes ---
    if uploaded_recettes_combo:
        found_recettes = False
        found_ing_recettes = False
        for file in uploaded_recettes_combo:
            name = file.name
            try:
                df = pd.read_csv(file, encoding="utf-8")
                if "Recettes.csv" in name:
                    dataframes["Recettes"] = df
                    found_recettes = True
                    st.sidebar.success("Recettes.csv chargé.")
                elif "Ingredients_recettes.csv" in name:
                    dataframes["Ingredients_recettes"] = df
                    found_ing_recettes = True
                    st.sidebar.success("Ingredients_recettes.csv chargé.")
            except Exception as e:
                st.sidebar.error(f"Erreur chargement {name}: {e}")
                all_files_uploaded = False
        if not found_recettes:
            st.sidebar.warning("Recettes.csv manquant.")
            all_files_uploaded = False
        if not found_ing_recettes:
            st.sidebar.warning("Ingredients_recettes.csv manquant.")
            all_files_uploaded = False
    else:
        all_files_uploaded = False

    # ------------------- Ingredients -------------------
    if uploaded_ingredients is not None:
        try:
            df = pd.read_csv(uploaded_ingredients, encoding="utf-8")
            dataframes["Ingredients"] = df
            st.sidebar.success("Ingredients.csv chargé.")
        except Exception as e:
            st.sidebar.error(f"Erreur chargement Ingredients.csv: {e}")
            all_files_uploaded = False
    else:
        all_files_uploaded = False

    # --- traitement Planning + Menus ---
    if uploaded_planning_menus:
        found_planning = False
        found_menus = False
        for file in uploaded_planning_menus:
            name = file.name
            try:
                if "Planning.csv" in name:
                    df = pd.read_csv(
                        file,
                        encoding="utf-8",
                        sep=";",
                        parse_dates=["Date"],
                        dayfirst=True,
                    )
                    dataframes["Planning"] = df
                    found_planning = True
                    st.sidebar.success("Planning.csv chargé.")
                elif "Menus.csv" in name:
                    df = pd.read_csv(file, encoding="utf-8")
                    dataframes["Menus"] = df
                    found_menus = True
                    st.sidebar.success("Menus.csv chargé.")
            except Exception as e:
                st.sidebar.error(f"Erreur chargement {name}: {e}")
                all_files_uploaded = False
        if not found_planning:
            st.sidebar.warning("Planning.csv manquant.")
            all_files_uploaded = False
        if not found_menus:
            st.sidebar.warning("Menus.csv manquant.")
            all_files_uploaded = False
    else:
        all_files_uploaded = False

    # ----------------------------------------------------------------
    #        VÉRIFICATION DES COLONNES ESSENTIELLES (inchangée)
    # ----------------------------------------------------------------
    if all_files_uploaded:
        try:
            verifier_colonnes(
                dataframes["Recettes"],
                [
                    COLONNE_ID_RECETTE,
                    COLONNE_NOM,
                    COLONNE_TEMPS_TOTAL,
                    COLONNE_AIME_PAS_PRINCIP,
                    "Transportable",
                    "Calories",
                    "Proteines",
                ],
                "Recettes.csv",
            )
            verifier_colonnes(
                dataframes["Planning"],
                ["Date", "Participants", "Transportable", "Temps", "Nutrition"],
                "Planning.csv",
            )
            verifier_colonnes(
                dataframes["Menus"], ["Date", "Recette"], "Menus.csv"
            )
            verifier_colonnes(
                dataframes["Ingredients"],
                [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"],
                "Ingredients.csv",
            )
            verifier_colonnes(
                dataframes["Ingredients_recettes"],
                [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"],
                "Ingredients_recettes.csv",
            )
        except ValueError:
            st.error(
                "Des colonnes essentielles sont manquantes. Vérifiez vos CSV."
            )
            return
    else:
        st.warning("Veuillez charger tous les fichiers requis.")
        return

    # ----------------------------------------------------------------
    #                     GÉNÉRATION DU MENU
    # ----------------------------------------------------------------
    st.markdown("---")
    st.header("1. Générer le Menu")
    st.write(
        "Cliquez sur le bouton ci-dessous pour générer le menu hebdomadaire "
        "et la liste de courses."
    )

    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération du menu en cours…"):
            try:
                # Conversion de colonnes numériques (si nécessaire)
                if "Temps_total" in dataframes["Recettes"].columns:
                    dataframes["Recettes"]["Temps_total"] = pd.to_numeric(
                        dataframes["Recettes"]["Temps_total"],
                        errors="coerce"
                    ).fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)

                # Initialisation du générateur de menus
                menu_generator = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                )

                df_menu_genere, liste_courses = menu_generator.generer_menu()

                st.success("🎉 Menu généré avec succès !")

                # ---------------- Affichage menu ----------------
                st.header("2. Menu Généré")
                st.dataframe(df_menu_genere)

                # Préparation export CSV
                df_export = df_menu_genere.copy()
                df_export = df_export.rename(
                    columns={
                        "Participant(s)": "Participant(s)",
                        COLONNE_NOM: "Nom",
                        "Date": "Date",
                    }
                )
                df_export["Date"] = pd.to_datetime(
                    df_export["Date"],
                    format="%d/%m/%Y %H:%M",
                    errors="coerce",
                ).dt.strftime("%Y-%m-%d %H:%M")
                df_export = df_export[["Date", "Participant(s)", "Nom"]]
                csv_menu = df_export.to_csv(
                    index=False, sep=",", encoding="utf-8-sig"
                )
                st.download_button(
                    label="📥 Télécharger le menu en CSV",
                    data=csv_menu,
                    file_name="menu_genere.csv",
                    mime="text/csv",
                )

                # ---------------- Liste de courses -------------
                st.header("3. Liste de Courses (Ingrédients manquants)")
                if liste_courses:
                    liste_courses_df = pd.DataFrame(
                        {"Ingrédient et Quantité": liste_courses}
                    )
                    st.dataframe(liste_courses_df)
                    csv_courses = liste_courses_df.to_csv(
                        index=False, sep=";", encoding="utf-8-sig"
                    )
                    st.download_button(
                        label="Télécharger la liste de courses (CSV)",
                        data=csv_courses,
                        file_name="liste_courses.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("Aucun ingrédient manquant identifié.")
            except Exception as e:
                st.error(
                    f"Une erreur est survenue lors de la génération : {e}"
                )
                logger.exception("Erreur génération menu")

# ------------------------------------------------------------------
if __name__ == "__main__":
    main()
