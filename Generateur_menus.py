# ───────────────────────────────────────────────
#  Générateur de Menus et Liste de Courses
#  Version "3 boutons" – 100 % redémarré de zéro
# ───────────────────────────────────────────────
import streamlit as st
import pandas as pd
import logging
from datetime import timedelta

# ──────────────── Constantes ────────────────
NB_JOURS_ANTI_REPETITION = 42
COLONNE_ID_RECETTE      = "Page_ID"
COLONNE_ID_INGREDIENT   = "Page_ID"
COLONNE_NOM             = "Nom"
COLONNE_TEMPS_TOTAL     = "Temps_total"
VALEUR_DEFAUT_TEMPS_PREP = 10
TEMPS_MAX_EXPRESS       = 20
TEMPS_MAX_RAPIDE        = 30
REPAS_EQUILIBRE         = 700

# ──────────────── Log ────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ──────────────── Utilitaire simple ────────────────
def verifier_colonnes(df, colonnes, nom):
    manq = [c for c in colonnes if c not in df.columns]
    if manq:
        raise ValueError(f"{nom} : colonnes manquantes {manq}")

# ──────────────── RecetteManager (ultra-résumé) ────────────────
class RecetteManager:
    def __init__(self, df_recettes, df_ing, df_ing_rec):
        self.df_recettes = df_recettes.set_index(COLONNE_ID_RECETTE)
        self.df_ing = df_ing
        self.df_ing_rec = df_ing_rec
    # … insérez ICI vos méthodes complètes si besoin …

# ──────────────── MenuGenerator (ultra-résumé) ────────────────
class MenuGenerator:
    def __init__(self, df_hist, df_recettes, df_plan,
                 df_ing, df_ing_rec):
        self.plan  = df_plan.copy()
        self.hist  = df_hist.copy()
        self.rm    = RecetteManager(df_recettes, df_ing, df_ing_rec)
        # … reste de la logique inchangé …

    def generer_menu(self):
        # -> dummy minimal : renvoie DataFrame vide + liste vide
        return pd.DataFrame(columns=["Date", COLONNE_NOM,
                                     "Participant(s)"]), []

# ──────────────── Interface Streamlit ────────────────
def main():
    st.set_page_config(page_title="Générateur de Menus",
                       layout="wide")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    # ----------- 3 boutons d’upload ----------- #
    st.sidebar.header("Chargement des CSV")

    up_recettes = st.sidebar.file_uploader(
        "1️⃣  Uploader Recettes.csv",
        type="csv")

    up_ing_group = st.sidebar.file_uploader(
        "2️⃣  Uploader Ingredients.csv ET Ingredients_recettes.csv",
        type="csv",
        accept_multiple_files=True)

    up_plan_group = st.sidebar.file_uploader(
        "3️⃣  Uploader Planning.csv ET Menus.csv",
        type="csv",
        accept_multiple_files=True)

    # ----------- Lecture fichiers ----------- #
    dfs = {}
    ok = True

    # Recettes
    if up_recettes:
        df = pd.read_csv(up_recettes, encoding="utf-8")
        if "Temps_total" in df.columns:
            df["Temps_total"] = pd.to_numeric(
                df["Temps_total"], errors="coerce"
            ).fillna(VALEUR_DEFAUT_TEMPS_PREP).astype(int)
        dfs["Recettes"] = df
    else:
        ok = False

    # Ingrédients + Ingrédients_recettes
    if up_ing_group and len(up_ing_group) == 2:
        for f in up_ing_group:
            if "Ingredients_recettes.csv" in f.name:
                dfs["Ingredients_recettes"] = pd.read_csv(f, encoding="utf-8")
            elif "Ingredients.csv" in f.name:
                dfs["Ingredients"] = pd.read_csv(f, encoding="utf-8")
        ok &= {"Ingredients", "Ingredients_recettes"} <= dfs.keys()
    else:
        ok = False

    # Planning + Menus
    if up_plan_group and len(up_plan_group) == 2:
        for f in up_plan_group:
            if "Planning.csv" in f.name:
                dfs["Planning"] = pd.read_csv(
                    f, encoding="utf-8", sep=";",
                    parse_dates=["Date"], dayfirst=True)
            elif "Menus.csv" in f.name:
                dfs["Menus"] = pd.read_csv(f, encoding="utf-8")
        ok &= {"Planning", "Menus"} <= dfs.keys()
    else:
        ok = False

    if not ok:
        st.info("Veuillez charger les 5 fichiers requis.")
        st.stop()

    # ----------- Vérification colonnes clefs ----------- #
    try:
        verifier_colonnes(dfs["Recettes"],
                          [COLONNE_ID_RECETTE, COLONNE_NOM,
                           COLONNE_TEMPS_TOTAL],
                          "Recettes.csv")
        verifier_colonnes(dfs["Ingredients"],
                          [COLONNE_ID_INGREDIENT, "Nom",
                           "Qte reste", "unité"],
                          "Ingredients.csv")
        verifier_colonnes(dfs["Ingredients_recettes"],
                          [COLONNE_ID_RECETTE, "Ingrédient ok",
                           "Qté/pers_s"],
                          "Ingredients_recettes.csv")
        verifier_colonnes(dfs["Planning"],
                          ["Date", "Participants",
                           "Transportable", "Temps", "Nutrition"],
                          "Planning.csv")
        verifier_colonnes(dfs["Menus"],
                          ["Date", "Recette"],
                          "Menus.csv")
    except ValueError as e:
        st.error(str(e))
        st.stop()

    st.markdown("---")
    st.header("1. Générer le Menu")

    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération en cours…"):
            gen = MenuGenerator(dfs["Menus"], dfs["Recettes"],
                                dfs["Planning"], dfs["Ingredients"],
                                dfs["Ingredients_recettes"])
            df_menu, courses = gen.generer_menu()

        st.success("Menu généré !")
        st.header("2. Menu")
        st.dataframe(df_menu)

        csv_menu = df_menu.to_csv(index=False, encoding="utf-8-sig")
        st.download_button("📥 Télécharger le menu CSV",
                           csv_menu, "menu_genere.csv",
                           mime="text/csv")

        st.header("3. Liste de Courses")
        if courses:
            df_courses = pd.DataFrame({"Ingrédient et Quantité": courses})
            st.dataframe(df_courses)
            st.download_button(
                "📥 Télécharger la liste CSV",
                df_courses.to_csv(index=False, sep=";",
                                  encoding="utf-8-sig"),
                "liste_courses.csv", "text/csv")
        else:
            st.info("Aucun ingrédient manquant.")

# ──────────────── Lancement ────────────────
if __name__ == "__main__":
    main()
