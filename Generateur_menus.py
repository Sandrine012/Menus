import streamlit as st
import pandas as pd
import logging
from datetime import timedelta

# ────────────────────── Constantes ──────────────────────
NB_JOURS_ANTI_REPETITION = 42
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"      # Même colonne pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID"   # Pour Ingredients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"
VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE  = 30
REPAS_EQUILIBRE   = 700

# ────────────────────── Logger ──────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ────────────────────── Fonctions utilitaires ──────────────────────
def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    manquantes = [c for c in colonnes_attendues if c not in df.columns]
    if manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {manquantes}")

# ────────────────────── Classe RecetteManager (inchangée) ──────────────────────
class RecetteManager:
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        if COLONNE_ID_RECETTE in self.df_recettes.columns:
            self.df_recettes.set_index(COLONNE_ID_RECETTE, inplace=True, drop=False)

        self.df_ingredients_initial  = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        self.stock_simule = self.df_ingredients_initial.copy()
        if "Qte reste" in self.stock_simule.columns:
            self.stock_simule["Qte reste"] = pd.to_numeric(
                self.stock_simule["Qte reste"], errors="coerce"
            ).fillna(0.0)
        else:
            self.stock_simule["Qte reste"] = 0.0

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

    # … (toutes les autres méthodes de RecetteManager restent identiques)
    # Pour économiser de l’espace ici, elles n’ont pas été recollées
    # Insérez-les sans les modifier si vous faisiez déjà appel à ces méthodes.
    # ----------------------------------------------------------------

# ────────────────────── Classe MenuGenerator (inchangée) ──────────────────────
class MenuGenerator:
    def __init__(self, df_menus_hist, df_recettes, df_planning,
                 df_ingredients, df_ingredients_recettes):
        # Copie des DataFrames et premières validations
        self.df_planning = df_planning.copy()
        if "Date" not in self.df_planning.columns:
            raise ValueError("Colonne 'Date' manquante dans Planning.csv")
        self.df_planning["Date"] = pd.to_datetime(
            self.df_planning["Date"], errors="coerce"
        )
        self.df_planning.dropna(subset=["Date"], inplace=True)

        self.recette_mgr = RecetteManager(
            df_recettes, df_ingredients, df_ingredients_recettes
        )
        self.df_menus_hist = df_menus_hist.copy()
        self.df_menus_hist["Date"] = pd.to_datetime(
            self.df_menus_hist["Date"], errors="coerce"
        )
        self.df_menus_hist.dropna(subset=["Date"], inplace=True)
        self.df_menus_hist["Semaine"] = self.df_menus_hist["Date"].dt.isocalendar().week

        self.ingredients_a_acheter = {}

    # … (toutes les méthodes de génération de menus restent identiques)
    # ----------------------------------------------------------------

# ────────────────────── Interface Streamlit ──────────────────────
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    # ── Barre latérale : chargement des fichiers ──────────────────
    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Veuillez charger tous les fichiers CSV nécessaires.")

    # 1️⃣ Recettes.csv
    uploaded_recettes = st.sidebar.file_uploader(
        "Uploader Recettes.csv",
        type="csv",
        key="recettes_uploader"
    )

    # 2️⃣ Ingredients.csv + Ingredients_recettes.csv
    uploaded_ingredients_files = st.sidebar.file_uploader(
        "Uploader Ingredients.csv et Ingredients_recettes.csv (sélectionnez les deux)",
        type="csv",
        accept_multiple_files=True,
        key="ingredients_combined_uploader"
    )

    # 3️⃣ Planning.csv + Menus.csv
    uploaded_planning_menus = st.sidebar.file_uploader(
        "Uploader Planning.csv et Menus.csv (sélectionnez les deux)",
        type="csv",
        accept_multiple_files=True,
        key="planning_menus_uploader"
    )

    # ── Lecture des fichiers ──────────────────────────────────────
    data = {}
    ok = True

    # Recettes.csv
    if uploaded_recettes:
        try:
            df = pd.read_csv(uploaded_recettes, encoding="utf-8")
            if "Temps_total" in df.columns:
                df["Temps_total"] = pd.to_numeric(
                    df["Temps_total"], errors="coerce"
                ).fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)
            if "Calories" in df.columns:
                df["Calories"] = pd.to_numeric(df["Calories"], errors="coerce")
            data["Recettes"] = df
            st.sidebar.success("Recettes.csv chargé")
        except Exception as e:
            st.sidebar.error(f"Erreur Recettes.csv : {e}")
            ok = False
    else:
        ok = False

    # Ingredients.csv + Ingredients_recettes.csv
    found_ing, found_ingrec = False, False
    if uploaded_ingredients_files:
        for f in uploaded_ingredients_files:
            try:
                df = pd.read_csv(f, encoding="utf-8")
                if "Ingredients.csv" in f.name:
                    data["Ingredients"] = df
                    found_ing = True
                    st.sidebar.success("Ingredients.csv chargé")
                elif "Ingredients_recettes.csv" in f.name:
                    data["Ingredients_recettes"] = df
                    found_ingrec = True
                    st.sidebar.success("Ingredients_recettes.csv chargé")
                else:
                    st.sidebar.warning(f"Ignoré : {f.name}")
            except Exception as e:
                st.sidebar.error(f"{f.name} : {e}")
                ok = False
        if not (found_ing and found_ingrec):
            st.sidebar.warning("Veuillez charger Ingredients.csv ET Ingredients_recettes.csv")
            ok = False
    else:
        ok = False

    # Planning.csv + Menus.csv
    found_plan, found_menu = False, False
    if uploaded_planning_menus:
        for f in uploaded_planning_menus:
            try:
                if "Planning.csv" in f.name:
                    data["Planning"] = pd.read_csv(
                        f, encoding="utf-8", sep=";", parse_dates=["Date"], dayfirst=True
                    )
                    found_plan = True
                    st.sidebar.success("Planning.csv chargé")
                elif "Menus.csv" in f.name:
                    data["Menus"] = pd.read_csv(f, encoding="utf-8")
                    found_menu = True
                    st.sidebar.success("Menus.csv chargé")
            except Exception as e:
                st.sidebar.error(f"{f.name} : {e}")
                ok = False
        if not (found_plan and found_menu):
            st.sidebar.warning("Veuillez charger Planning.csv ET Menus.csv")
            ok = False
    else:
        ok = False

    # Validation finale
    if not ok:
        st.warning("Tous les fichiers requis n’ont pas été chargés.")
        st.stop()

    try:
        verifier_colonnes(
            data["Recettes"],
            [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL,
             COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories"],
            "Recettes.csv"
        )
        verifier_colonnes(
            data["Ingredients"],
            [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"],
            "Ingredients.csv"
        )
        verifier_colonnes(
            data["Ingredients_recettes"],
            [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"],
            "Ingredients_recettes.csv"
        )
        verifier_colonnes(
            data["Planning"],
            ["Date", "Participants", "Transportable", "Temps", "Nutrition"],
            "Planning.csv"
        )
        verifier_colonnes(
            data["Menus"],
            ["Date", "Recette"],
            "Menus.csv"
        )
    except ValueError:
        st.stop()

    # ── Génération du menu ─────────────────────────────────────────
    st.markdown("---")
    st.header("1. Générer le Menu")
    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération en cours…"):
            try:
                gen = MenuGenerator(
                    data["Menus"], data["Recettes"], data["Planning"],
                    data["Ingredients"], data["Ingredients_recettes"]
                )
                df_menu, liste_courses = gen.generer_menu()

                st.success("Menu généré !")
                st.header("2. Menu Généré")
                st.dataframe(df_menu)

                # Export CSV du menu
                df_export = df_menu.copy()
                df_export["Date"] = pd.to_datetime(
                    df_export["Date"], dayfirst=True, errors="coerce"
                ).dt.strftime("%Y-%m-%d %H:%M")
                csv_menu = df_export[["Date", "Participant(s)", "Nom"]].to_csv(
                    index=False, encoding="utf-8-sig"
                )
                st.download_button(
                    "📥 Télécharger le menu (CSV)",
                    data=csv_menu,
                    file_name="menu_genere.csv",
                    mime="text/csv"
                )

                # Liste de courses
                st.header("3. Liste de Courses")
                if liste_courses:
                    df_courses = pd.DataFrame(
                        {"Ingrédient et Quantité": liste_courses}
                    )
                    st.dataframe(df_courses)
                    csv_courses = df_courses.to_csv(
                        index=False, sep=";", encoding="utf-8-sig"
                    )
                    st.download_button(
                        "📥 Télécharger la liste de courses (CSV)",
                        data=csv_courses,
                        file_name="liste_courses.csv",
                        mime="text/csv"
                    )
                else:
                    st.info("Aucun ingrédient manquant !")
            except Exception as e:
                st.error(f"Erreur : {e}")
                logger.exception(e)

# ────────────────────── Lancement ──────────────────────
if __name__ == "__main__":
    main()
