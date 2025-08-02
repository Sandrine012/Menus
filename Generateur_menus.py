import streamlit as st
import pandas as pd
import logging
from datetime import datetime, timedelta

# ────────────── CONSTANTES GLOBALES ──────────────
NB_JOURS_ANTI_REPETITION = 42
COLONNE_NOM             = "Nom"
COLONNE_TEMPS_TOTAL     = "Temps_total"
COLONNE_ID_RECETTE      = "Page_ID"
COLONNE_ID_INGREDIENT   = "Page_ID"
COLONNE_AIME_PAS_PRINCIP= "Aime_pas_princip"
VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS       = 20
TEMPS_MAX_RAPIDE        = 30
REPAS_EQUILIBRE         = 700

# ────────────── LOGGER ──────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ────────────── UTILITAIRES ──────────────
def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    manquantes = [c for c in colonnes_attendues if c not in df.columns]
    if manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {manquantes}")

# ────────────── CLASSE RecetteManager ──────────────
class RecetteManager:
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        if COLONNE_ID_RECETTE in self.df_recettes.columns:
            self.df_recettes.set_index(COLONNE_ID_RECETTE, inplace=True, drop=False)

        self.df_ingredients_initial  = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        # Mise en place d’un « stock simulé »
        self.stock_simule = self.df_ingredients_initial.copy()
        if "Qte reste" in self.stock_simule.columns:
            self.stock_simule["Qte reste"] = pd.to_numeric(
                self.stock_simule["Qte reste"], errors="coerce"
            ).fillna(0.0)
        else:
            logger.error("'Qte reste' manquante dans Ingredients.csv")
            self.stock_simule["Qte reste"] = 0.0

        # Liste des ingrédients « anti-gaspi »
        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

    # Trouve les ingrédients dont le stock est « élevé »
    def _trouver_ingredients_stock_eleve(self):
        seuil_gr = 100
        seuil_pc = 1
        ingredients_stock = {}

        colonnes_ok = ["Qte reste", "unité", COLONNE_ID_INGREDIENT, "Nom"]
        if not all(c in self.stock_simule.columns for c in colonnes_ok):
            logger.warning("Colonnes manquantes dans stock_simule.")
            return {}

        for _, row in self.stock_simule.iterrows():
            try:
                qte   = float(str(row["Qte reste"]).replace(",", "."))
                unite = str(row["unité"]).lower()
                pid   = str(row[COLONNE_ID_INGREDIENT])

                if (unite in ["gr", "g", "ml", "cl"] and qte >= seuil_gr) or \
                   (unite in ["pc", "tranches"] and qte >= seuil_pc):
                    ingredients_stock[pid] = row["Nom"]
            except Exception as e:
                logger.debug(f"Erreur analyse stock : {e}")
        return ingredients_stock

    # ---------- autres méthodes publ. ---------------
    def get_ingredients_for_recipe(self, recette_id):
        rid = str(recette_id)
        mask = self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str) == rid
        return self.df_ingredients_recettes.loc[mask,
            ["Ingrédient ok", "Qté/pers_s"]].to_dict("records")

    def recette_utilise_ingredient_anti_gaspi(self, recette_id):
        return any(
            str(ing["Ingrédient ok"]) in self.anti_gaspi_ingredients
            for ing in self.get_ingredients_for_recipe(recette_id)
        )

    def obtenir_nom(self, recette_id):
        rid = str(recette_id)
        if rid in self.df_recettes.index:
            return self.df_recettes.at[rid, COLONNE_NOM]
        return f"Recette_ID_{rid}"

    def est_transportable(self, recette_id):
        rid = str(recette_id)
        try:
            val = str(self.df_recettes.at[rid, "Transportable"]).lower()
            return val == "oui"
        except Exception:
            return False

    def obtenir_temps_preparation(self, recette_id):
        rid = str(recette_id)
        try:
            return int(self.df_recettes.at[rid, COLONNE_TEMPS_TOTAL])
        except Exception:
            return VALEUR_DEFAUT_TEMPS_PREPARATION

    # ----------- évaluation stock / manquants --------------
    def calculer_quantite_necessaire(self, recette_id, nb_personnes):
        res = {}
        for ing in self.get_ingredients_for_recipe(recette_id):
            try:
                ing_id = str(ing["Ingrédient ok"])
                qte    = float(str(ing["Qté/pers_s"]).replace(",", "."))
                res[ing_id] = qte * nb_personnes
            except Exception:
                continue
        return res

    def evaluer_disponibilite_et_manquants(self, recette_id, nb_personnes):
        besoins = self.calculer_quantite_necessaire(recette_id, nb_personnes)
        if not besoins:
            return 0, 0, {}

        total  = len(besoins)
        dispo  = 0
        score  = 0
        manque = {}

        for ing_id, qte_need in besoins.items():
            mask = self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == str(ing_id)
            qte_en_stock = self.stock_simule.loc[mask, "Qte reste"].sum()
            ratio = min(1.0, qte_en_stock / qte_need) if qte_need else 0
            score += ratio
            if ratio >= 0.3:
                dispo += 1
            if qte_en_stock < qte_need:
                manque[ing_id] = qte_need - qte_en_stock

        return score / total, (dispo / total) * 100, manque

    def decrementer_stock(self, recette_id, nb_personnes, date_repas):
        besoins = self.calculer_quantite_necessaire(recette_id, nb_personnes)
        for ing_id, qte_need in besoins.items():
            mask = self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == str(ing_id)
            if not mask.any(): continue
            idx = self.stock_simule.index[mask][0]
            actuel = self.stock_simule.at[idx, "Qte reste"]
            self.stock_simule.at[idx, "Qte reste"] = max(0, actuel - qte_need)
        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

# ────────────── CLASSE MenuGenerator (simplifiée) ──────────────
class MenuGenerator:
    def __init__(self, df_hist, df_recettes, df_planning,
                 df_ingredients, df_ingredients_recettes):
        self.df_planning = df_planning.copy()
        self.df_planning["Date"] = pd.to_datetime(
            self.df_planning["Date"], errors="coerce"
        )
        self.df_planning.dropna(subset=["Date"], inplace=True)

        self.rm = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.df_hist = df_hist.copy()
        self.df_hist["Date"] = pd.to_datetime(self.df_hist["Date"], errors="coerce")

        self.a_acheter = {}

    def compter_participants(self, code):
        return 1 if code == "B" else len([c for c in str(code).split(",") if c.strip()])

    def generer_menu(self):
        resultats = []
        for _, row in self.df_planning.sort_values("Date").iterrows():
            date, participants, transportable = row["Date"], row["Participants"], str(row["Transportable"]).lower()
            nb_pers = self.compter_participants(participants)

            # Filtre très basique : première recette valide
            recette_id_choose = None
            for rid in self.rm.df_recettes.index.astype(str):
                if transportable == "oui" and not self.rm.est_transportable(rid):
                    continue
                score, pct, manque = self.rm.evaluer_disponibilite_et_manquants(rid, nb_pers)
                if score >= 0.5:      # moitié des ingrédients en stock
                    recette_id_choose = rid
                    for mid, qte in manque.items():
                        self.a_acheter[mid] = self.a_acheter.get(mid, 0) + qte
                    self.rm.decrementer_stock(rid, nb_pers, date)
                    break

            nom_plat = self.rm.obtenir_nom(recette_id_choose) if recette_id_choose else "Aucune recette trouvée"
            resultats.append({
                "Date": date.strftime("%d/%m/%Y %H:%M"),
                COLONNE_NOM: nom_plat,
                "Participant(s)": participants,
                "Remarques": "-" if recette_id_choose else "Pas de recette disponible"
            })

        df_menu = pd.DataFrame(resultats)

        # Mise en forme de la liste de courses
        courses = []
        for ing_id, qte in self.a_acheter.items():
            nom = ing_id
            try:
                nom = self.rm.df_ingredients_initial.loc[
                    self.rm.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str)==str(ing_id),
                    "Nom"
                ].iloc[0]
            except Exception:
                pass
            courses.append(f"{nom}: {qte:.2f}")

        return df_menu, courses

# ────────────── INTERFACE STREAMLIT ──────────────
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Veuillez charger tous les fichiers nécessaires.")

    # 1️⃣ Recettes.csv
    up_recettes = st.sidebar.file_uploader("Uploader Recettes.csv", type="csv")

    # 2️⃣ Ingredients.csv + Ingredients_recettes.csv
    up_ing = st.sidebar.file_uploader(
        "Uploader Ingredients.csv ET Ingredients_recettes.csv",
        type="csv",
        accept_multiple_files=True
    )

    # 3️⃣ Planning.csv + Menus.csv
    up_plan_menu = st.sidebar.file_uploader(
        "Uploader Planning.csv ET Menus.csv",
        type="csv",
        accept_multiple_files=True
    )

    # ---------- Lecture fichiers ----------
    dfs = {}
    ok = True

    # Recettes
    if up_recettes:
        dfs["Recettes"] = pd.read_csv(up_recettes, encoding="utf-8")
        if "Temps_total" in dfs["Recettes"].columns:
            dfs["Recettes"]["Temps_total"] = pd.to_numeric(
                dfs["Recettes"]["Temps_total"], errors="coerce"
            ).fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)
    else:
        ok = False

    # Ingrédients
    if up_ing:
        for f in up_ing:
            if "Ingredients.csv" in f.name:
                dfs["Ingredients"] = pd.read_csv(f, encoding="utf-8")
            elif "Ingredients_recettes.csv" in f.name:
                dfs["Ingredients_recettes"] = pd.read_csv(f, encoding="utf-8")
        if not {"Ingredients", "Ingredients_recettes"} <= dfs.keys():
            ok = False
    else:
        ok = False

    # Planning + Menus
    if up_plan_menu:
        for f in up_plan_menu:
            if "Planning.csv" in f.name:
                dfs["Planning"] = pd.read_csv(f, encoding="utf-8", sep=";", parse_dates=["Date"], dayfirst=True)
            elif "Menus.csv" in f.name:
                dfs["Menus"] = pd.read_csv(f, encoding="utf-8")
        if not {"Planning", "Menus"} <= dfs.keys():
            ok = False
    else:
        ok = False

    if not ok:
        st.warning("Tous les fichiers requis ne sont pas encore chargés.")
        st.stop()

    # Vérification colonnes clés
    try:
        verifier_colonnes(dfs["Recettes"],
            [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable"],
            "Recettes.csv")
        verifier_colonnes(dfs["Ingredients"],
            [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"],
            "Ingredients.csv")
        verifier_colonnes(dfs["Ingredients_recettes"],
            [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"],
            "Ingredients_recettes.csv")
        verifier_colonnes(dfs["Planning"],
            ["Date", "Participants", "Transportable", "Temps", "Nutrition"],
            "Planning.csv")
        verifier_colonnes(dfs["Menus"],
            ["Date", "Recette"],
            "Menus.csv")
    except ValueError:
        st.stop()

    st.markdown("---")
    st.header("1. Générer le Menu")

    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération…"):
            mg = MenuGenerator(
                dfs["Menus"], dfs["Recettes"], dfs["Planning"],
                dfs["Ingredients"], dfs["Ingredients_recettes"]
            )
            df_menu, courses = mg.generer_menu()
            st.success("Menu généré !")

            # Affichage
            st.header("2. Menu Généré")
            st.dataframe(df_menu)

            # Téléchargement menu
            csv_menu = df_menu.to_csv(index=False, encoding="utf-8-sig")
            st.download_button("📥 Télécharger le menu (CSV)",
                               csv_menu, "menu_genere.csv", "text/csv")

            # Liste de courses
            st.header("3. Liste de Courses")
            if courses:
                df_courses = pd.DataFrame({"Ingrédient et Quantité": courses})
                st.dataframe(df_courses)
                csv_courses = df_courses.to_csv(index=False, sep=";", encoding="utf-8-sig")
                st.download_button("📥 Télécharger la liste (CSV)",
                                   csv_courses, "liste_courses.csv", "text/csv")
            else:
                st.info("Aucun ingrédient manquant !")

# ────────────── MAIN ──────────────
if __name__ == "__main__":
    main()
