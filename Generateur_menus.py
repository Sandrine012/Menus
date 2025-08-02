# ================================================================
#           GÉNÉRATEUR DE MENUS & LISTE DE COURSES
#                  (version 3 boutons upload)
# ================================================================

import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta

# ----------------------------------------------------------------
#                 CONSTANTES & PARAMÈTRES GLOBAUX
# ----------------------------------------------------------------
NB_JOURS_ANTI_REPETITION = 42         # éviter de reproposer une recette < 6 semaines
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"        # même nom pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID"     # ID unique des ingrédients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"
VALEUR_DEFAUT_TEMPS_PREPARATION = 10  # si la durée est vide
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700                 # seuil indicatif de calories/repas

# ----------------------------------------------------------------
#                       CONFIGURATION LOGGER
# ----------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s",
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------------------
#                        OUTILS UTILITAIRES
# ----------------------------------------------------------------
def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    """Vérifie la présence des colonnes attendues dans un DataFrame."""
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier} : {', '.join(colonnes_manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier} : {colonnes_manquantes}")

# ----------------------------------------------------------------
#                       CLASSES MÉTIER
# ----------------------------------------------------------------
class RecetteManager:
    """Gère le filtrage et la sélection des recettes."""
    def __init__(self, df_recettes, df_ing_recettes):
        self.recettes = df_recettes.copy()
        self.ing_recettes = df_ing_recettes.copy()

    def recettes_disponibles(self, aime_pas, delai_repetition, aujourd_hui, menus_history):
        """Filtre les recettes selon : exclusions, anti-répétition, etc."""
        logger.debug("Filtrage des recettes…")
        df = self.recettes.copy()

        # 1. Exclure ingrédients détestés
        if aime_pas:
            pattern = "|".join(aime_pas)
            df = df[~df[COLONNE_AIME_PAS_PRINCIP].str.contains(pattern, case=False, na=False)]

        # 2. Anti-répétition sur NB_JOURS_ANTI_REPETITION
        if not menus_history.empty:
            dejavu = menus_history[
                menus_history["Date"] >= aujourd_hui - timedelta(days=delai_repetition)
            ][COLONNE_NOM].unique()
            df = df[~df[COLONNE_NOM].isin(dejavu)]

        logger.debug("Recettes restantes : %d", len(df))
        return df

    def choisir_recette(self, df_filtre):
        """Retourne une recette aléatoire parmi le DataFrame filtré."""
        if df_filtre.empty:
            raise ValueError("Aucune recette disponible après filtrage.")
        return df_filtre.sample(1).iloc[0]

class MenusHistoryManager:
    """Charge et met à jour l’historique des menus."""
    def __init__(self, df_menus):
        self.df_menus = df_menus.copy()

    def ajouter_menu(self, date, recette):
        nouvelle_ligne = pd.DataFrame({"Date": [date], COLONNE_NOM: [recette]})
        self.df_menus = pd.concat([self.df_menus, nouvelle_ligne], ignore_index=True)

class MenuGenerator:
    """Génère un menu hebdomadaire équilibré et la liste de courses associée."""
    def __init__(self, df_menus, df_recettes, df_planning, df_ingredients, df_ing_recettes):
        self.history = MenusHistoryManager(df_menus)
        self.recette_mgr = RecetteManager(df_recettes, df_ing_recettes)
        self.planning = df_planning
        self.ingredients_stock = df_ingredients
        self.ing_recettes = df_ing_recettes

    def generer_menu(self):
        aujourd_hui = datetime.today().normalize()
        menu_genere = []
        liste_courses = []

        for _, ligne in self.planning.iterrows():
            date_repas = ligne["Date"]
            aime_pas = ligne.get("Aime_pas", "").split(",") if "Aime_pas" in ligne else []

            # Filtrer les recettes admissibles
            recettes_ok = self.recette_mgr.recettes_disponibles(
                aime_pas,
                NB_JOURS_ANTI_REPETITION,
                aujourd_hui,
                self.history.df_menus,
            )
            recette_choisie = self.recette_mgr.choisir_recette(recettes_ok)

            # Mettre à jour historique
            self.history.ajouter_menu(date_repas, recette_choisie[COLONNE_NOM])

            # Ajouter au résultat
            menu_genere.append(
                {
                    "Date": date_repas.strftime("%d/%m/%Y"),
                    "Participant(s)": ligne.get("Participants", ""),
                    COLONNE_NOM: recette_choisie[COLONNE_NOM],
                }
            )

            # Vérifier ingrédients manquants
            ing_needed = self.ing_recettes[self.ing_recettes[COLONNE_ID_RECETTE] == recette_choisie[COLONNE_ID_RECETTE]]
            for _, ing in ing_needed.iterrows():
                id_ing = ing["Ingrédient ok"]
                qte_requise = ing["Qté/pers_s"] * max(ligne.get("Participants", 1), 1)
                stock_row = self.ingredients_stock[self.ingredients_stock[COLONNE_ID_INGREDIENT] == id_ing]
                if stock_row.empty or stock_row.iloc[0]["Qte reste"] < qte_requise:
                    liste_courses.append(f"{id_ing} : {qte_requise}")

        df_menu_genere = pd.DataFrame(menu_genere)
        return df_menu_genere, sorted(liste_courses)

# ----------------------------------------------------------------
#                        INTERFACE STREAMLIT
# ----------------------------------------------------------------
def main():
    st.set_page_config(page_title="Générateur de Menus", layout="wide")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Charge les cinq CSV nécessaires : Recettes, Ingredients_recettes, Ingredients, Planning, Menus.")

    # ----------- 1. Bouton combiné Recettes + Ingredients_recettes -----------
    fichiers_recettes_combo = st.sidebar.file_uploader(
        "Uploader Recettes.csv et Ingredients_recettes.csv",
        type="csv",
        accept_multiple_files=True,
        key="recettes_combo",
    )

    # -------------------- 2. Bouton Ingredients --------------------
    fichier_ingredients = st.sidebar.file_uploader(
        "Uploader Ingredients.csv",
        type="csv",
        key="ingredients_file",
    )

    # ----------- 3. Bouton combiné Planning + Menus ----------------
    fichiers_planning_menus = st.sidebar.file_uploader(
        "Uploader Planning.csv et Menus.csv",
        type="csv",
        accept_multiple_files=True,
        key="planning_menus_combo",
    )

    # ----------------------------------------------------------------
    #                  LECTURE DES FICHIERS & VALIDATION
    # ----------------------------------------------------------------
    dfs = {}
    tout_ok = True

    # --- Traitement Recettes + Ingredients_recettes ---
    if fichiers_recettes_combo:
        for f in fichiers_recettes_combo:
            nom = f.name.lower()
            try:
                df = pd.read_csv(f, encoding="utf-8")
                if "recettes" in nom:
                    dfs["Recettes"] = df
                    st.sidebar.success("Recettes.csv chargé.")
                elif "ingredients_recettes" in nom:
                    dfs["Ingredients_recettes"] = df
                    st.sidebar.success("Ingredients_recettes.csv chargé.")
            except Exception as e:
                st.sidebar.error(f"Erreur chargement {nom} : {e}")
                tout_ok = False
        # Vérifier présence des deux fichiers
        if "Recettes" not in dfs or "Ingredients_recettes" not in dfs:
            st.sidebar.warning("Il manque Recettes.csv ou Ingredients_recettes.csv.")
            tout_ok = False
    else:
        tout_ok = False

    # ------------------- Ingredients -------------------
    if fichier_ingredients:
        try:
            dfs["Ingredients"] = pd.read_csv(fichier_ingredients, encoding="utf-8")
            st.sidebar.success("Ingredients.csv chargé.")
        except Exception as e:
            st.sidebar.error(f"Erreur chargement Ingredients.csv : {e}")
            tout_ok = False
    else:
        tout_ok = False

    # --- Traitement Planning + Menus ---
    if fichiers_planning_menus:
        for f in fichiers_planning_menus:
            nom = f.name.lower()
            try:
                if "planning" in nom:
                    dfs["Planning"] = pd.read_csv(
                        f, sep=";", encoding="utf-8", parse_dates=["Date"], dayfirst=True
                    )
                    st.sidebar.success("Planning.csv chargé.")
                elif "menus" in nom:
                    dfs["Menus"] = pd.read_csv(f, encoding="utf-8")
                    st.sidebar.success("Menus.csv chargé.")
            except Exception as e:
                st.sidebar.error(f"Erreur chargement {nom} : {e}")
                tout_ok = False
        if "Planning" not in dfs or "Menus" not in dfs:
            st.sidebar.warning("Il manque Planning.csv ou Menus.csv.")
            tout_ok = False
    else:
        tout_ok = False

    # ----------------------------------------------------------------
    #               VÉRIFICATION DES COLONNES ESSENTIELLES
    # ----------------------------------------------------------------
    if tout_ok:
        try:
            verifier_colonnes(
                dfs["Recettes"],
                [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories", "Proteines"],
                "Recettes.csv",
            )
            verifier_colonnes(dfs["Ingredients"], [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"], "Ingredients.csv")
            verifier_colonnes(dfs["Ingredients_recettes"], [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingrédients_recettes.csv")
            verifier_colonnes(dfs["Planning"], ["Date", "Participants", "Transportable", "Temps", "Nutrition"], "Planning.csv")
            verifier_colonnes(dfs["Menus"], ["Date", "Recette"], "Menus.csv")
        except ValueError:
            tout_ok = False

    if not tout_ok:
        st.warning("Veuillez charger correctement les cinq fichiers avant de continuer.")
        return

    # ----------------------------------------------------------------
    #                     GÉNÉRATION DU MENU
    # ----------------------------------------------------------------
    st.markdown("---")
    st.header("1. Générer le Menu")

    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération en cours…"):
            try:
                # Harmoniser le type numérique de Temps_total
                dfs["Recettes"][COLONNE_TEMPS_TOTAL] = pd.to_numeric(
                    dfs["Recettes"][COLONNE_TEMPS_TOTAL],
                    errors="coerce",
                ).fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)

                gen = MenuGenerator(
                    dfs["Menus"],
                    dfs["Recettes"],
                    dfs["Planning"],
                    dfs["Ingredients"],
                    dfs["Ingredients_recettes"],
                )
                df_menu, liste_courses = gen.generer_menu()

                st.success("🎉 Menu généré !")
                st.header("2. Menu Généré")
                st.dataframe(df_menu)

                # Export menu
                csv_menu = df_menu.to_csv(index=False, encoding="utf-8-sig")
                st.download_button(
                    "📥 Télécharger le menu (CSV)",
                    data=csv_menu,
                    file_name="menu_genere.csv",
                    mime="text/csv",
                )

                # Liste de courses
                st.header("3. Liste de Courses (ingrédients manquants)")
                if liste_courses:
                    df_courses = pd.DataFrame({"Ingrédient et Quantité": liste_courses})
                    st.dataframe(df_courses)
                    csv_courses = df_courses.to_csv(index=False, sep=";", encoding="utf-8-sig")
                    st.download_button(
                        "Télécharger la liste de courses (CSV)",
                        data=csv_courses,
                        file_name="liste_courses.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("Aucun ingrédient manquant 🎉")
            except Exception as e:
                st.error(f"Erreur lors de la génération : {e}")
                logger.exception("Erreur génération menu")

# ----------------------------------------------------------------
if __name__ == "__main__":
    main()
