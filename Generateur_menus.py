import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta

# Configuration du logger pour Streamlit
# Niveau DEBUG pour voir les détails de filtrage
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# Constantes globales
NB_JOURS_ANTI_REPETITION = 42

COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID" # Utilisé comme ID pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID" # Utilisé comme ID pour Ingredients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"

VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700

def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    """Vérifie si toutes les colonnes attendues sont présentes dans le DataFrame."""
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(colonnes_manquantes)}")
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}")

class RecetteManager:
    def __init__(self, recettes_df, ingredients_df, ingredients_recettes_df):
        self.recettes_df = recettes_df
        self.ingredients_df = ingredients_df
        self.ingredients_recettes_df = ingredients_recettes_df
        self._validate_dataframes()

    def _validate_dataframes(self):
        verifier_colonnes(self.recettes_df, ["Page_ID", "Nom", "Saison", "Calories", "Proteines", "Temps_total", "Aime_pas_princip", "Type_plat", "Transportable"], "Recettes.csv")
        verifier_colonnes(self.ingredients_df, ["Page_ID", "Nom"], "Ingredients.csv")
        verifier_colonnes(self.ingredients_recettes_df, ["ID_recette", "ID_ingredient", "Quantite", "Unite"], "Ingredients_recettes.csv")

    def get_recette_by_id(self, recette_id):
        return self.recettes_df[self.recettes_df[COLONNE_ID_RECETTE] == recette_id].iloc[0]

    def get_recette_nom_by_id(self, recette_id):
        recette = self.recettes_df[self.recettes_df[COLONNE_ID_RECETTE] == recette_id]
        return recette[COLONNE_NOM].iloc[0] if not recette.empty else f"Recette_ID_{recette_id}"

    def get_all_recettes_ids(self):
        return self.recettes_df[COLONNE_ID_RECETTE].tolist()

    def get_nom_plat(self, recette_id):
        recette = self.recettes_df[self.recettes_df[COLONNE_ID_RECETTE] == recette_id]
        if not recette.empty:
            return recette[COLONNE_NOM].iloc[0]
        return f"Recette_ID_{recette_id}"

    def est_transportable(self, recette_id):
        recette = self.recettes_df[self.recettes_df[COLONNE_ID_RECETTE] == recette_id]
        if not recette.empty:
            # Assurez-vous que la colonne 'Transportable' est lue correctement
            # et que les valeurs sont traitées comme des chaînes de caractères.
            transportable_value = str(recette['Transportable'].iloc[0]).strip().lower()
            return transportable_value == "oui"
        return False

    def get_ingredients_for_recette(self, recette_id):
        ingredients_ids = self.ingredients_recettes_df[
            self.ingredients_recettes_df['ID_recette'] == recette_id
        ]['ID_ingredient']
        ingredients_details = self.ingredients_df[
            self.ingredients_df[COLONNE_ID_INGREDIENT].isin(ingredients_ids)
        ]
        return ingredients_details[COLONNE_NOM].tolist()

class MenuGenerator:
    def __init__(self, df_planning, recettes_df, ingredients_df, ingredients_recettes_df, menus_historique_df=None):
        self.df_planning = df_planning
        self.recette_manager = RecetteManager(recettes_df, ingredients_df, ingredients_recettes_df)
        self.menus_historique_df = menus_historique_df if menus_historique_df is not None else pd.DataFrame()
        self.plats_transportables_semaine = {} # {date_repas: {nom_plat, plat_id_orig, date_repas}}
        self.repas_b_utilises_ids_list = [] # Liste des IDs de plats utilisés pour les repas B dans cette génération

        # Convertir la colonne Date du df_planning en datetime, gérant le format "DD/MM/YYYY HH:MM"
        self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], format="%d/%m/%Y %H:%M", errors='coerce')
        # Supprimer les lignes où la date n'a pas pu être parsée
        self.df_planning.dropna(subset=['Date'], inplace=True)
        # S'assurer que les colonnes sont des chaînes de caractères pour éviter des erreurs inattendues
        self.df_planning['Participants'] = self.df_planning['Participants'].astype(str).str.strip()
        self.df_planning['Transportable'] = self.df_planning['Transportable'].astype(str).str.strip()
        self.df_planning['Temps'] = self.df_planning['Temps'].astype(str).str.strip()
        self.df_planning['Nutrition'] = self.df_planning['Nutrition'].astype(str).str.strip()

        # Nettoyage des données 'nan' converties en 'nan' string
        self.df_planning.replace('nan', '', inplace=True)

    def generer_menu(self):
        df_menu_genere = pd.DataFrame(columns=[
            'Date', 'Jour', 'Moment', 'Participant(s)', 'Type de repas requis',
            'Nutrition requise', 'Nom', 'Page_ID'
        ])
        liste_courses = {}

        for idx, row in self.df_planning.iterrows():
            date_repas_dt = row['Date']
            date_repas_str = date_repas_dt.strftime('%Y-%m-%d %H:%M')
            jour_semaine = date_repas_dt.strftime('%A')
            moment_repas = date_repas_dt.strftime('%H:%M')
            participants_str = row['Participants']
            transportable_req = row['Transportable']
            temps_req = row['Temps']
            nutrition_req = row['Nutrition']

            nom_plat_final = "" # Initialisation
            recette_choisie_id = None # Initialisation

            if participants_str == "B":
                logger.info(f"--- Traitement Repas B: {date_repas_str} ---")
                nom_plat_final, recette_choisie_id = self.generer_menu_repas_b(date_repas_dt)
                if recette_choisie_id:
                    self.repas_b_utilises_ids_list.append(recette_choisie_id)
                logger.debug(f"Repas B généré: {nom_plat_final} (ID: {recette_choisie_id}).")
            else:
                logger.info(f"--- Traitement Repas standard: {date_repas_str} ---")
                candidats_recettes = self._filtrer_recettes(
                    participants_str, temps_req, nutrition_req, date_repas_dt
                )

                if candidats_recettes:
                    recette_choisie_id = random.choice(candidats_recettes)
                    nom_plat_final = self.recette_manager.get_nom_plat(recette_choisie_id)
                    logger.debug(f"Recette finale sélectionnée pour repas standard: {nom_plat_final} ({recette_choisie_id}).")

                    # Ajouter le plat aux plats transportables si requis et possible
                    if transportable_req == "oui" and self.recette_manager.est_transportable(recette_choisie_id):
                        self.plats_transportables_semaine[date_repas_dt] = {
                            'nom_plat_reste': nom_plat_final,
                            'plat_id_orig_str': recette_choisie_id,
                            'date_repas': date_repas_dt
                        }
                        logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) ajouté à plats_transportables_semaine pour le {date_repas_dt.strftime('%Y-%m-%d')}.")
                    else:
                        logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) non ajouté à plats_transportables_semaine (transportable_req est '{transportable_req}' ou recette non transportable).")
                else:
                    nom_plat_final = "Aucune recette trouvée"
                    logger.warning(f"Aucune recette trouvée pour le repas du {date_repas_str} avec les critères donnés.")

            # Mise à jour du DataFrame final du menu
            df_menu_genere.loc[idx, 'Date'] = date_repas_str
            df_menu_genere.loc[idx, 'Jour'] = jour_semaine
            df_menu_genere.loc[idx, 'Moment'] = moment_repas
            df_menu_genere.loc[idx, 'Participant(s)'] = participants_str
            df_menu_genere.loc[idx, 'Type de repas requis'] = temps_req
            df_menu_genere.loc[idx, 'Nutrition requise'] = nutrition_req
            df_menu_genere.loc[idx, 'Nom'] = nom_plat_final
            df_menu_genere.loc[idx, 'Page_ID'] = recette_choisie_id

            # Mettre à jour la liste de courses
            if recette_choisie_id:
                ingredients_recette = self.recette_manager.get_ingredients_for_recette(recette_choisie_id)
                for ingredient in ingredients_recette:
                    liste_courses[ingredient] = liste_courses.get(ingredient, 0) + 1


        return df_menu_genere, liste_courses

    def _filtrer_recettes(self, participants_str, temps_req, nutrition_req, date_repas):
        recettes_filtrees = self.recette_manager.recettes_df.copy()

        # 1. Filtrer par personnes qui n'aiment pas (Aime_pas_princip)
        if participants_str:
            participants_list = [p.strip() for p in participants_str.split(',') if p.strip()]
            for participant in participants_list:
                recettes_filtrees = recettes_filtrees[
                    ~recettes_filtrees[COLONNE_AIME_PAS_PRINCIP].str.contains(participant, na=False)
                ]
                logger.debug(f"Filtré par personne ({participant}): {len(recettes_filtrees)} recettes restantes.")

        # 2. Filtrer par temps de préparation (Temps_total)
        if temps_req:
            temps_req_lower = temps_req.lower()
            if temps_req_lower == 'express':
                recettes_filtrees = recettes_filtrees[recettes_filtrees[COLONNE_TEMPS_TOTAL] <= TEMPS_MAX_EXPRESS]
                logger.debug(f"Filtré par temps (Express <= {TEMPS_MAX_EXPRESS} min): {len(recettes_filtrees)} recettes restantes.")
            elif temps_req_lower == 'rapide':
                recettes_filtrees = recettes_filtrees[recettes_filtrees[COLONNE_TEMPS_TOTAL] <= TEMPS_MAX_RAPIDE]
                logger.debug(f"Filtré par temps (Rapide <= {TEMPS_MAX_RAPIDE} min): {len(recettes_filtrees)} recettes restantes.")
            # Si vide, on ne filtre pas par temps

        # 3. Filtrer par nutrition (Calories)
        if nutrition_req and nutrition_req.lower() == 'equilibré':
            recettes_filtrees = recettes_filtrees[recettes_filtrees['Calories'] <= REPAS_EQUILIBRE]
            logger.debug(f"Filtré par nutrition (Équilibré <= {REPAS_EQUILIBRE} kcal): {len(recettes_filtrees)} recettes restantes.")

        # 4. Filtrer par anti-répétition (menus_historique_df)
        if not self.menus_historique_df.empty:
            date_limite = date_repas - timedelta(days=NB_JOURS_ANTI_REPETITION)
            plats_recents = self.menus_historique_df[
                self.menus_historique_df['Date'] >= date_limite
            ]['Nom'].tolist()
            # Nettoyer les noms des plats récents pour la comparaison (prendre le premier mot)
            first_words_used = {word.split()[0].lower() for word in plats_recents if word}

            recettes_avant_anti_rep = len(recettes_filtrees)
            recettes_filtrees = recettes_filtrees[
                ~recettes_filtrees[COLONNE_NOM].apply(lambda x: x.split()[0].lower() in first_words_used)
            ]
            logger.debug(f"Filtré par anti-répétition (derniers {NB_JOURS_ANTI_REPETITION} jours): {recettes_avant_anti_rep - len(recettes_filtrees)} recettes supprimées. {len(recettes_filtrees)} restantes.")


        return recettes_filtrees[COLONNE_ID_RECETTE].tolist()

    def generer_menu_repas_b(self, date_repas):
        candidats_restes = []
        logger.debug(f"--- Recherche de restes pour Repas B le {date_repas.strftime('%Y-%m-%d %H:%M')} ---")

        # Supprimer les restes trop vieux pour ne pas encombrer
        dates_a_retirer = [
            d for d in self.plats_transportables_semaine if (date_repas - d).days > 2 # Garde 1 et 2 jours
        ]
        for d in dates_a_retirer:
            logger.debug(f"Suppression du reste du {d.strftime('%Y-%m-%d')} car trop ancien.")
            del self.plats_transportables_semaine[d]

        for date_plat_orig, reste_info in self.plats_transportables_semaine.items():
            nom_plat_reste = reste_info['nom_plat_reste']
            plat_id_orig_str = reste_info['plat_id_orig_str']

            jours_ecoules = (date_repas - date_plat_orig).days
            logger.debug(f"Éval reste {nom_plat_reste} (ID: {plat_id_orig_str}) du {date_plat_orig.strftime('%Y-%m-%d')}. Jours écoulés: {jours_ecoules}.")

            # Condition 1: Le reste doit avoir été fait 1 ou 2 jours avant le repas B
            if not (0 < jours_ecoules <= 2):
                logger.debug(f"Reste {nom_plat_reste} filtré: Jours écoulés ({jours_ecoules}) hors de la plage (1-2 jours).")
                continue

            # Condition 2: Le reste ne doit pas avoir déjà été utilisé pour un Repas B dans cette génération
            if plat_id_orig_str in self.repas_b_utilises_ids_list:
                logger.debug(f"Reste {nom_plat_reste} filtré: Déjà utilisé pour un repas B.")
                continue

            # Condition 3: Le nom du plat ne doit pas être générique (ex: Recette_ID_...)
            if nom_plat_reste.startswith("Recette_ID_") or not nom_plat_reste:
                logger.debug(f"Reste {nom_plat_reste} filtré: Nom de plat invalide ou générique.")
                continue

            # Condition 4: S'assurer que la recette est toujours marquée comme transportable dans Recettes.csv
            if not self.recette_manager.est_transportable(plat_id_orig_str):
                logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) filtré: La recette d'origine n'est pas marquée comme transportable dans Recettes.csv.")
                continue

            candidats_restes.append(reste_info)

        candidat_reste_choisi = None
        if candidats_restes:
            # Trier par ancienneté (le plus ancien en premier)
            candidats_restes.sort(key=lambda x: x['date_repas'])
            candidat_reste_choisi = candidats_restes[0] # Sélectionner le plus ancien

            logger.info(f"Reste choisi pour Repas B: {candidat_reste_choisi['nom_plat_reste']} (ID: {candidat_reste_choisi['plat_id_orig_str']}).")
            # Le plat sera ajouté à repas_b_utilises_ids_list dans la fonction appelante (generer_menu)
            return candidat_reste_choisi['nom_plat_reste'], candidat_reste_choisi['plat_id_orig_str']
        else:
            logger.info("Aucun reste disponible ou sélectionné.")
            return "Pas de reste disponible", None


def load_data(uploaded_files):
    dataframes = {}
    for file_name, key, expected_columns, delimiter in [
        ("Recettes.csv", "Recettes", ["Page_ID", "Nom", "Saison", "Calories", "Proteines", "Temps_total", "Aime_pas_princip", "Type_plat", "Transportable"], ","),
        ("Ingredients.csv", "Ingredients", ["Page_ID", "Nom"], ","),
        ("Ingredients_recettes.csv", "Ingredients_recettes", ["ID_recette", "ID_ingredient", "Quantite", "Unite"], ","),
        ("Planning.csv", "Planning", ["Date", "Participants", "Transportable", "Temps", "Nutrition"], ";"),
        ("Menus.csv", "Menus_Historique", ["Date", "Nom"], ";") # Historique des menus, peut être vide au début
    ]:
        if file_name in uploaded_files:
            try:
                # Lire le fichier et essayer de détecter l'encodage
                df = pd.read_csv(uploaded_files[file_name], sep=delimiter, encoding='utf-8')
                logger.debug(f"Fichier {file_name} lu avec encodage utf-8.")
            except UnicodeDecodeError:
                df = pd.read_csv(uploaded_files[file_name], sep=delimiter, encoding='latin-1')
                logger.debug(f"Fichier {file_name} lu avec encodage latin-1.")
            except Exception as e:
                st.error(f"Erreur lors de la lecture de {file_name}: {e}")
                logger.exception(f"Erreur de lecture de {file_name}")
                continue

            # Assurer que les colonnes sont des strings pour éviter les problèmes de comparaison de types
            for col in expected_columns:
                if col in df.columns:
                    # Ne pas convertir 'Date' ou les colonnes numériques en str trop tôt si elles sont utilisées pour calculs
                    # Mais s'assurer que les colonnes de texte sont des strings pour les filtres.
                    if col not in ["Date", "Calories", "Proteines", "Temps_total", "Quantite"]:
                         df[col] = df[col].astype(str).str.strip()
            
            # Pour la colonne 'Transportable', remplacer les NaN par des chaînes vides ou 'non'
            if 'Transportable' in df.columns:
                df['Transportable'] = df['Transportable'].fillna('non').astype(str).str.strip().str.lower()
            
            # Gérer les colonnes numériques qui pourraient être lues comme objets
            for col_num in ["Calories", "Proteines", "Temps_total", "Quantite"]:
                if col_num in df.columns:
                    df[col_num] = pd.to_numeric(df[col_num], errors='coerce').fillna(0) # Convertir en numérique, NaN à 0


            verifier_colonnes(df, expected_columns, file_name)
            dataframes[key] = df
        else:
            if file_name != "Menus.csv": # Menus.csv peut être optionnel
                st.warning(f"Fichier {file_name} manquant. Veuillez le téléverser.")
            else:
                dataframes[key] = pd.DataFrame(columns=expected_columns) # Créer un DataFrame vide pour l'historique

    return dataframes

def main():
    st.set_page_config(layout="wide")
    st.title("🍽️ Générateur de Menus et Listes de Courses")

    st.sidebar.header("1. Téléversez vos fichiers CSV")
    uploaded_files_map = {}
    for f in ["Recettes.csv", "Ingredients.csv", "Ingredients_recettes.csv", "Planning.csv", "Menus.csv"]:
        uploaded_file = st.sidebar.file_uploader(f"Téléverser {f}", type="csv", key=f)
        if uploaded_file:
            uploaded_files_map[f] = uploaded_file

    dataframes = load_data(uploaded_files_map)

    if ("Recettes" in dataframes and "Ingredients" in dataframes and
            "Ingredients_recettes" in dataframes and "Planning" in dataframes):

        if st.sidebar.button("Générer le Menu"):
            try:
                menu_generator = MenuGenerator(
                    dataframes["Planning"],
                    dataframes["Recettes"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"],
                    dataframes.get("Menus_Historique") # Menus_Historique est optionnel
                )
                df_menu_genere, liste_courses = menu_generator.generer_menu()

                st.success("🎉 Menu généré avec succès !")

                # LIGNE DE DÉBOGAGE AJOUTÉE ICI
                st.header("Debug: Contenu de df_menu_genere avant affichage")
                st.write(df_menu_genere)
                # FIN LIGNE DE DÉBOGAGE

                st.header("2. Menu Généré")
                st.dataframe(df_menu_genere)

                st.header("3. Liste de Courses (Ingrédients manquants cumulés)")
                if liste_courses:
                    liste_courses_df = pd.DataFrame(liste_courses.items(), columns=["Ingrédient", "Quantité manquante"])
                    st.dataframe(liste_courses_df)

                    csv = liste_courses_df.to_csv(index=False, sep=';', encoding='utf-8-sig')
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
