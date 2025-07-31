import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
import re
from datetime import datetime
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from notion_client.helpers import get_id

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes globales ---
FICHIER_SORTIE_MENU_CSV = "Menus_generes.csv"
FICHIER_SORTIE_LISTES_TXT = "Listes_ingredients.txt"
FICHIER_EXPORT_NOTION_CSV = "Menus_extraits_Notion.csv" # Pour l'extraction Notion

# Noms de colonnes utilisés dans les DataFrames (doivent correspondre aux noms de vos propriétés Notion ou CSV)
COLONNE_ID_RECETTE = "ID Recette"
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps total (min)"
COLONNE_AIME_PAS_PRINCIP = "n'aime pas (principal)"
COLONNE_ID_INGREDIENT = "ID Ingrédient"
COLONNE_DATE_PLANNING = "Date" # Nom de la colonne dans le Planning CSV
COLONNE_PARTICIPANTS_PLANNING = "Participants" # Nom de la colonne dans le Planning CSV

# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"] # Base de données pour les menus générés/historique
    # DATABASE_ID_PLANNING n'est plus nécessaire car Planning vient d'un CSV uploadé

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Le secret Notion '{e.args[0]}' n'est pas configuré. "
             "Veuillez les ajouter dans le fichier .streamlit/secrets.toml ou dans Streamlit Cloud.")
    st.stop()
except Exception as e:
    st.error(f"Erreur de connexion à Notion : {e}")
    st.stop()


# --- Fonctions d'extraction de données Notion génériques ---

@st.cache_data(ttl=3600) # Cache les données pendant 1 heure
def get_notion_data_to_dataframe(database_id, columns_mapping, date_cols=None):
    """
    Extrait les données d'une base de données Notion spécifique
    et les formate en DataFrame Pandas.

    Args:
        database_id (str): L'ID de la base de données Notion.
        columns_mapping (dict): Un dictionnaire mappant les noms de propriétés Notion
                                aux noms de colonnes du DataFrame souhaités.
        date_cols (list, optional): Une liste de noms de colonnes de date à convertir.
    
    Returns:
        pd.DataFrame: Un DataFrame contenant les données extraites.
    """
    if date_cols is None:
        date_cols = []

    results = []
    has_more = True
    next_cursor = None

    logger.info(f"Début de l'extraction de la base de données Notion: {database_id}")

    try:
        while has_more:
            query_payload = {
                "database_id": database_id,
                "page_size": 100
            }
            if next_cursor:
                query_payload["start_cursor"] = next_cursor

            response = notion.databases.query(**query_payload)
            results.extend(response['results'])
            has_more = response['has_more']
            next_cursor = response['next_cursor']
            logger.info(f"Pages extraites : {len(results)}. Has more: {has_more}")
            if has_more:
                time.sleep(0.1) # Petite pause pour éviter de surcharger l'API

        data = []
        for page in results:
            props = page['properties']
            row_data = {}
            for notion_prop, df_col in columns_mapping.items():
                prop_type = props.get(notion_prop, {}).get("type")
                value = None

                if prop_type == "title":
                    value = props.get(notion_prop, {}).get("title", [])[0].get("plain_text") if props.get(notion_prop, {}).get("title") else None
                elif prop_type == "rich_text":
                    value = "".join([t.get("plain_text") for t in props.get(notion_prop, {}).get("rich_text", [])])
                elif prop_type == "number":
                    value = props.get(notion_prop, {}).get("number")
                elif prop_type == "multi_select":
                    value = ", ".join([opt.get("name") for opt in props.get(notion_prop, {}).get("multi_select", [])])
                elif prop_type == "select":
                    value = props.get(notion_prop, {}).get("select", {}).get("name")
                elif prop_type == "checkbox":
                    value = props.get(notion_prop, {}).get("checkbox")
                elif prop_type == "date":
                    value = props.get(notion_prop, {}).get("date", {}).get("start") if props.get(notion_prop, {}).get("date") else None
                elif prop_type == "relation":
                    # Pour les relations, nous extrayons les IDs
                    value = [rel.get("id") for rel in props.get(notion_prop, {}).get("relation", [])]
                    # Si c'est une relation simple pour un ID, prenez le premier
                    if len(value) == 1:
                        value = value[0]
                    elif not value:
                        value = None # Pas de relation
                    else:
                        value = str(value) # Si plusieurs relations ou complexité, laisser en string de liste d'IDs
                # Ajoutez d'autres types de propriétés Notion si nécessaire
                
                row_data[df_col] = value
            data.append(row_data)

        df = pd.DataFrame(data)

        # Conversion des colonnes de date
        for col in date_cols:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce')
        
        logger.info(f"Extraction de la base de données {database_id} terminée. {len(df)} entrées extraites.")
        return df

    except RequestTimeoutError:
        logger.error(f"La requête Notion pour {database_id} a expiré. Veuillez réessayer.")
        st.error(f"La requête Notion pour {database_id} a expiré. Veuillez réessayer.")
        return pd.DataFrame()
    except APIResponseError as e:
        logger.error(f"Erreur de l'API Notion pour {database_id}: {e.code} - {e.message}")
        st.error(f"Erreur de l'API Notion pour {database_id}: {e.code} - {e.message}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'extraction Notion de {database_id}: {e}", exc_info=True)
        st.error(f"Une erreur inattendue est survenue lors de l'extraction de {database_id}: {e}")
        return pd.DataFrame()


# --- Fonctions d'extraction spécifiques aux bases de données ---

def get_recettes_data():
    """Extrait les données des recettes depuis Notion."""
    recettes_columns_mapping = {
        "ID Recette": COLONNE_ID_RECETTE,
        "Nom de la recette": COLONNE_NOM,
        "Temps total (min)": COLONNE_TEMPS_TOTAL,
        "Transportable": "Transportable",
        "n'aime pas (principal)": COLONNE_AIME_PAS_PRINCIP,
        # Ajoutez toutes les autres colonnes de recettes pertinentes
    }
    df = get_notion_data_to_dataframe(DATABASE_ID_RECETTES, recettes_columns_mapping)
    return df

def get_ingredients_data():
    """Extrait les données des ingrédients (stock) depuis Notion."""
    ingredients_columns_mapping = {
        "ID Ingrédient": COLONNE_ID_INGREDIENT,
        "Nom de l'ingrédient": COLONNE_NOM,
        "Quantité restante": "Qte reste", # Nom de la colonne pour le stock
        "Unité": "unité", # Nom de la colonne pour l'unité
        # Ajoutez d'autres colonnes de stock si nécessaire
    }
    df = get_notion_data_to_dataframe(DATABASE_ID_INGREDIENTS, ingredients_columns_mapping)
    return df

def get_ingredients_recettes_data():
    """Extrait les données des ingrédients par recette depuis Notion (relation)."""
    ingredients_recettes_columns_mapping = {
        "ID Recette": COLONNE_ID_RECETTE, # Relation vers la base de données Recettes
        "Ingrédient lié": "Ingrédient ok", # Relation vers la base de données Ingrédients
        "Quantité par personne": "Qté/pers_s", # Quantité par personne
        # Ajoutez d'autres colonnes si nécessaire
    }
    df = get_notion_data_to_dataframe(DATABASE_ID_INGREDIENTS_RECETTES, ingredients_recettes_columns_mapping)
    
    # Pour les relations, la fonction générique renvoie une liste d'IDs.
    # Si 'ID Recette' est une liste [ID], convertissez-la en ID unique.
    if COLONNE_ID_RECETTE in df.columns and df[COLONNE_ID_RECETTE].apply(lambda x: isinstance(x, list)).any():
        df[COLONNE_ID_RECETTE] = df[COLONNE_ID_RECETTE].apply(lambda x: x[0] if isinstance(x, list) and x else None)
    if 'Ingrédient ok' in df.columns and df['Ingrédient ok'].apply(lambda x: isinstance(x, list)).any():
        df['Ingrédient ok'] = df['Ingrédient ok'].apply(lambda x: x[0] if isinstance(x, list) and x else None)
    
    return df

def get_existing_menus_data():
    """
    Extrait l'historique des menus déjà enregistrés dans Notion (DATABASE_ID_MENUS).
    Cette fonction est déjà présente dans votre Generateur (4).py.
    """
    results = []
    has_more = True
    next_cursor = None

    logger.info(f"Début de l'extraction de l'historique des menus de la base de données Notion: {DATABASE_ID_MENUS}")

    try:
        while has_more:
            query_payload = {
                "database_id": DATABASE_ID_MENUS,
                "page_size": 100
            }
            if next_cursor:
                query_payload["start_cursor"] = next_cursor

            response = notion.databases.query(**query_payload)
            results.extend(response['results'])
            has_more = response['has_more']
            next_cursor = response['next_cursor']
            if has_more:
                time.sleep(0.1)

        data = []
        for page in results:
            props = page['properties']
            menu_data = {
                "ID Notion": page['id'],
                "Date": props.get("Date", {}).get("date", {}).get("start") if props.get("Date", {}).get("date") else None,
                "Nom Menu": props.get("Nom Menu", {}).get("title", [])[0].get("plain_text") if props.get("Nom Menu", {}).get("title") else None,
                "Participant(s)": ", ".join([p.get("name") for p in props.get("Participant(s)", {}).get("multi_select", [])]) if props.get("Participant(s)", {}).get("multi_select") else None,
            }
            data.append(menu_data)

        df = pd.DataFrame(data)
        if not df.empty:
            df["Date"] = pd.to_datetime(df["Date"], errors='coerce') # Garder en datetime pour MenuGenerator si nécessaire
            logger.info(f"Extraction terminée. {len(df)} menus extraits de l'historique.")
        return df

    except RequestTimeoutError:
        logger.error("La requête Notion pour l'historique des menus a expiré. Veuillez réessayer.")
        st.error("La requête Notion pour l'historique des menus a expiré. Veuillez réessayer.")
        return pd.DataFrame()
    except APIResponseError as e:
        logger.error(f"Erreur de l'API Notion pour l'historique des menus: {e.code} - {e.message}")
        st.error(f"Erreur de l'API Notion pour l'historique des menus: {e.code} - {e.message}")
        return pd.DataFrame()
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'extraction de l'historique des menus: {e}", exc_info=True)
        st.error(f"Une erreur inattendue est survenue lors de l'extraction de l'historique des menus: {e}")
        return pd.DataFrame()


# --- Fonctions pour la logique de génération de menus ( inchangées par rapport à la dernière réponse ) ---

def verifier_colonnes(df, colonnes_attendues, nom_fichier):
    """Vérifie si toutes les colonnes attendues sont présentes dans le DataFrame."""
    missing_cols = [col for col in colonnes_attendues if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {', '.join(missing_cols)}")


class RecetteManager:
    """Gère les recettes, les ingrédients et le stock simulé."""
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes
        self.df_ingredients_initial = df_ingredients.copy()
        self.stock_simule = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes

    def obtenir_nom(self, recette_id):
        if recette_id is None: return "Recette Inconnue"
        nom_recette = self.df_recettes.loc[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == str(recette_id), COLONNE_NOM]
        return nom_recette.iloc[0] if not nom_recette.empty else f"Recette_ID_{recette_id}"

    def obtenir_temps_preparation(self, recette_id):
        if recette_id is None: return 0.0
        temps = self.df_recettes.loc[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == str(recette_id), COLONNE_TEMPS_TOTAL]
        return temps.iloc[0] if not temps.empty and not pd.isna(temps.iloc[0]) else 0.0

    def est_transportable(self, recette_id):
        if recette_id is None or 'Transportable' not in self.df_recettes.columns: return False
        transportable = self.df_recettes.loc[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == str(recette_id), 'Transportable']
        return transportable.iloc[0] == True if not transportable.empty and not pd.isna(transportable.iloc[0]) else False

    def obtenir_nom_ingredient_par_id(self, ing_id):
        if ing_id is None: return "Ingrédient Inconnu"
        nom_ing = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == str(ing_id), COLONNE_NOM]
        return nom_ing.iloc[0] if not nom_ing.empty else f"ID_Ing_{ing_id}"

    def decrementer_stock(self, recette_id, participants_count, date_repas_dt):
        ingredients_consommes_ce_repas = set()
        ingredients_de_la_recette = self.df_ingredients_recettes[self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str) == str(recette_id)]
        
        if ingredients_de_la_recette.empty:
            logger.warning(f"Aucun ingrédient trouvé pour la recette ID: {recette_id}. Le stock ne sera pas décrémenté.")
            return []

        for _, row in ingredients_de_la_recette.iterrows():
            ing_id = str(row['Ingrédient ok'])
            qte_par_pers = row['Qté/pers_s']
            qte_necessaire = qte_par_pers * participants_count

            idx_stock = self.stock_simule[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id].index
            if not idx_stock.empty:
                idx_stock = idx_stock[0]
                qte_actuelle = self.stock_simule.loc[idx_stock, 'Qte reste']
                self.stock_simule.loc[idx_stock, 'Qte reste'] = max(0.0, qte_actuelle - qte_necessaire)
                ingredients_consommes_ce_repas.add(ing_id)
            else:
                logger.warning(f"Ingrédient ID '{ing_id}' de la recette '{recette_id}' non trouvé dans le stock simulé.")
        return list(ingredients_consommes_ce_repas)


class MenuGenerator:
    """Classe principale pour générer le planning de menus."""
    def __init__(self, df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes):
        self.df_menus_hist = df_menus_hist
        self.df_planning = df_planning
        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.ingredients_a_acheter_cumules = {}

    def _traiter_menu_standard(self, date_repas_dt, participants_str, participants_count,
                                used_recipes_current_generation_set, menu_recent_noms,
                                transportable_req, temps_req, nutrition_req):
        recettes_candidates = self.recette_manager.df_recettes.copy()
        
        recettes_candidates = recettes_candidates[~recettes_candidates[COLONNE_ID_RECETTE].astype(str).isin(used_recipes_current_generation_set)]
        recettes_candidates = recettes_candidates[~recettes_candidates[COLONNE_NOM].isin(menu_recent_noms)]

        if transportable_req:
            recettes_candidates = recettes_candidates[recettes_candidates['Transportable'] == True]
        if temps_req is not None:
            recettes_candidates = recettes_candidates[recettes_candidates[COLONNE_TEMPS_TOTAL] <= temps_req]

        if COLONNE_AIME_PAS_PRINCIP in recettes_candidates.columns:
            participants_list = [p.strip() for p in participants_str.split(',') if p.strip()]
            for participant in participants_list:
                recettes_candidates = recettes_candidates[~recettes_candidates[COLONNE_AIME_PAS_PRINCIP].astype(str).str.contains(participant, case=False, na=False)]
        
        if not recettes_candidates.empty:
            recette_id_choisie = recettes_candidates.iloc[0][COLONNE_ID_RECETTE]

            ingredients_manquants_rec = {}
            ingredients_de_la_recette = self.recette_manager.df_ingredients_recettes[self.recette_manager.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str) == str(recette_id_choisie)]

            for _, ing_rec_row in ingredients_de_la_recette.iterrows():
                ing_id = str(ing_rec_row['Ingrédient ok'])
                qte_requise = ing_rec_row['Qté/pers_s'] * participants_count

                qte_en_stock = self.recette_manager.stock_simule[self.recette_manager.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id]['Qte reste'].iloc[0] if ing_id in self.recette_manager.stock_simule[COLONNE_ID_INGREDIENT].astype(str).values else 0.0

                if qte_en_stock < qte_requise:
                    manquant = qte_requise - qte_en_stock
                    ingredients_manquants_rec[ing_id] = ingredients_manquants_rec.get(ing_id, 0.0) + manquant
            
            return recette_id_choisie, ingredients_manquants_rec
        else:
            return None, {}

    def _log_decision_recette(self, recette_id, date, participants):
        nom = self.recette_manager.obtenir_nom(recette_id)
        logger.info(f"Recette choisie: {nom} pour le {date.strftime('%Y-%m-%d %H:%M')} ({participants} pers.)")

    def _ajouter_resultat(self, resultats_df_list, date, nom_plat, participants, remarques, temps_prep, recette_id):
        resultats_df_list.append({
            COLONNE_DATE_PLANNING: date.strftime("%d/%m/%Y %H:%M"),
            "Nom Menu": nom_plat,
            COLONNE_PARTICIPANTS_PLANNING: participants,
            "Remarques": remarques,
            "Temps Préparation": temps_prep,
            COLONNE_ID_RECETTE: recette_id
        })

    def generer_menu(self, transportable_req=False, temps_req=None, nutrition_req=None):
        self.ingredients_a_acheter_cumules = {}
        self.recette_manager.stock_simule = self.recette_manager.df_ingredients_initial.copy()
        
        resultats_df_list = []
        used_recipes_current_generation_set = set()
        menu_recent_noms = []
        plats_transportables_semaine = {}
        ingredients_effectivement_utilises_ids_set = set()

        for index, row in self.df_planning.iterrows():
            date_repas_dt = row[COLONNE_DATE_PLANNING]
            participants_str = row[COLONNE_PARTICIPANTS_PLANNING]
            
            participants_count = len([p.strip() for p in participants_str.split(',') if p.strip()])
            if participants_count == 0:
                logger.warning(f"Aucun participant spécifié pour la date {date_repas_dt}. Ignoré.")
                self._ajouter_resultat(resultats_df_list, date_repas_dt, "Ignoré (Pas de participants)", participants_str, "Aucun participant spécifié.", 0, None)
                continue

            nom_plat_final = ""
            remarques_repas = ""
            temps_prep_final = 0.0
            recette_choisie_id = None
            ingredients_consommes_ce_repas = []
            
            recette_choisie_id, ingredients_manquants_pour_recette_choisie = self._traiter_menu_standard(
                date_repas_dt, participants_str, participants_count,
                used_recipes_current_generation_set, menu_recent_noms,
                transportable_req, temps_req, nutrition_req
            )
            
            if recette_choisie_id:
                nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                if nom_plat_final and "Recette_ID_" not in nom_plat_final:
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    used_recipes_current_generation_set.add(str(recette_choisie_id))

                    for ing_id_manquant, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                        self.ingredients_a_acheter_cumules[ing_id_manquant] = self.ingredients_a_acheter_cumules.get(ing_id_manquant, 0.0) + qte_manquante

                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)

                    if date_repas_dt.weekday() >= 5 and self.recette_manager.est_transportable(recette_choisie_id):
                        plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                    self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)
                else:
                    nom_plat_final = f"Recette ID {recette_choisie_id} - Nom Invalide"
                    remarques_repas = "Erreur: Nom de recette non trouvé pour cet ID."
                    recette_choisie_id = None
            else:
                nom_plat_final = "Pas de recette trouvée"
                remarques_repas = "Aucune recette candidate ne correspond aux critères."

            self._ajouter_resultat(resultats_df_list, date_repas_dt, nom_plat_final, participants_str, remarques_repas, temps_prep_final, recette_choisie_id)
            
            if nom_plat_final and "Pas de recette" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Erreur" not in nom_plat_final and "Invalide" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3: menu_recent_noms.pop(0)

            if ingredients_consommes_ce_repas:
                for ing_id_cons in ingredients_consommes_ce_repas:
                    if ing_id_cons and str(ing_id_cons).lower() not in ['nan', 'none', '']:
                        ingredients_effectivement_utilises_ids_set.add(str(ing_id_cons))

        df_stock_final_simule = self.recette_manager.stock_simule.copy()
        noms_ingredients_non_utilises_en_stock = []
        if COLONNE_ID_INGREDIENT in df_stock_final_simule.columns:
            stock_restant_positif_df = df_stock_final_simule[df_stock_final_simule['Qte reste'] > 0]
            ids_stock_restant_positif = set(stock_restant_positif_df[COLONNE_ID_INGREDIENT].astype(str))

            ids_ingredients_non_utilises = ids_stock_restant_positif - ingredients_effectivement_utilises_ids_set
            noms_ingredients_non_utilises_en_stock = sorted(list(filter(None, [self.recette_manager.obtenir_nom_ingredient_par_id(ing_id) for ing_id in ids_ingredients_non_utilises])))
        else:
            logger.error(f"'{COLONNE_ID_INGREDIENT}' non trouvé dans stock_simule final.")

        liste_courses_finale = []
        for ing_id_achat, qte_achat in self.ingredients_a_acheter_cumules.items():
            nom_ing_achat = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id_achat)
            if nom_ing_achat and "ID_Ing_" not in nom_ing_achat :
                unite_ing = "unité(s)"
                try:
                    unite_series = self.recette_manager.df_ingredients_initial.loc[self.recette_manager.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == str(ing_id_achat), 'unité']
                    if not unite_series.empty:
                        unite_ing = unite_series.iloc[0]
                except (IndexError, KeyError):
                    logger.warning(f"Unité non trouvée pour l'ingrédient {nom_ing_achat} (ID: {ing_id_achat}) pour la liste de courses.")

                liste_courses_finale.append(f"{nom_ing_achat}: {qte_achat:.2f} {unite_ing}")
            else:
                liste_courses_finale.append(f"ID Ingrédient {ing_id_achat}: {qte_achat:.2f} unité(s) (Nom non trouvé)")
        liste_courses_finale.sort()
        
        # Filtrer les ingrédients effectivement utilisés pour la liste des ingrédients utilisés
        ingredients_utilises_menu = sorted(list(filter(None, [self.recette_manager.obtenir_nom_ingredient_par_id(ing_id) for ing_id in ingredients_effectivement_utilises_ids_set])))


        return pd.DataFrame(resultats_df_list), ingredients_utilises_menu, noms_ingredients_non_utilises_en_stock, liste_courses_finale

# --- Fonctions d'intégration Notion (du code Generateur (4).py) ---

def integrate_with_notion(df_menus_genere, database_id):
    """
    Intègre les menus générés dans la base de données Notion spécifiée.
    """
    if df_menus_genere.empty:
        logger.warning("Aucun menu à intégrer dans Notion. DataFrame vide.")
        return

    logger.info(f"Début de l'intégration de {len(df_menus_genere)} menus dans la base de données Notion: {database_id}")

    try:
        for index, row in df_menus_genere.iterrows():
            date_str = row[COLONNE_DATE_PLANNING]
            try:
                date_iso = datetime.strptime(date_str, "%d/%m/%Y %H:%M").isoformat()
            except ValueError:
                logger.error(f"Format de date invalide pour {date_str}. Ignoré.")
                continue

            properties = {
                "Nom Menu": {
                    "title": [
                        {
                            "text": {
                                "content": row["Nom Menu"] if pd.notna(row["Nom Menu"]) else "Menu sans nom"
                            }
                        }
                    ]
                },
                "Date": {
                    "date": {
                        "start": date_iso
                    }
                },
                "Participant(s)": {
                    "multi_select": [
                        {"name": p.strip()} for p in row[COLONNE_PARTICIPANTS_PLANNING].split(',') if p.strip()
                    ]
                },
                "Temps Préparation": {
                    "number": float(row["Temps Préparation"]) if pd.notna(row["Temps Préparation"]) else 0
                },
                "Remarques": {
                    "rich_text": [
                        {
                            "text": {
                                "content": row["Remarques"] if pd.notna(row["Remarques"]) else ""
                            }
                        }
                    ]
                },
            }

            notion.pages.create(
                parent={"database_id": database_id},
                properties=properties
            )
            logger.info(f"Menu '{row['Nom Menu']}' intégré pour le {row[COLONNE_DATE_PLANNING]}.")
            time.sleep(0.1)

        st.success("Menus générés intégrés avec succès dans Notion !")

    except RequestTimeoutError:
        logger.error("La requête Notion a expiré lors de l'intégration. Veuillez réessayer.")
        st.error("La requête Notion a expiré lors de l'intégration. Veuillez réessayer.")
    except APIResponseError as e:
        logger.error(f"Erreur de l'API Notion lors de l'intégration: {e.code} - {e.message}")
        st.error(f"Erreur de l'API Notion lors de l'intégration: {e.code} - {e.message}")
    except Exception as e:
        logger.error(f"Erreur inattendue lors de l'intégration Notion: {e}", exc_info=True)
        st.error(f"Une erreur inattendue est survenue lors de l'intégration des menus: {e}")


# --- Application Streamlit principale ---

st.set_page_config(layout="wide", page_title="Générateur de Menus Automatisé avec Notion")

st.title("🍽️ Générateur de Menus Automatisé")
st.markdown("Bienvenue ! Cet outil vous aide à générer des plannings de menus et des listes de courses en utilisant vos données Notion et un fichier de planning local, puis à les réintégrer.")

# --- Section de chargement des données ---
st.header("1. Charger les données")

# Chargement du fichier Planning.csv
st.subheader("Charger le fichier Planning.csv :")
uploaded_planning_file = st.file_uploader("Choisissez votre fichier Planning.csv", type="csv", key="planning_uploader")
if uploaded_planning_file is not None:
    try:
        df_planning = pd.read_csv(uploaded_planning_file, sep=',', encoding='utf-8')
        verifier_colonnes(df_planning, [COLONNE_DATE_PLANNING, COLONNE_PARTICIPANTS_PLANNING], "Planning.csv")
        # Conversion des dates
        df_planning[COLONNE_DATE_PLANNING] = pd.to_datetime(df_planning[COLONNE_DATE_PLANNING], format="%d/%m/%Y %H:%M", errors='coerce')
        df_planning = df_planning.sort_values(by=COLONNE_DATE_PLANNING).reset_index(drop=True)
        st.session_state['df_planning'] = df_planning
        st.success("Fichier Planning.csv chargé avec succès !")
        st.write(f"Aperçu de Planning.csv ({len(df_planning)} lignes) :")
        st.dataframe(df_planning.head())
    except Exception as e:
        st.error(f"Erreur lors du chargement de Planning.csv : {e}. Assurez-vous que le fichier est bien un CSV valide avec les colonnes attendues.")
        logger.error(f"Erreur chargement Planning.csv: {e}", exc_info=True)
else:
    st.info("Veuillez charger votre fichier Planning.csv pour commencer.")

# Bouton de chargement des données Notion
st.subheader("Charger les données Recettes, Ingrédients et Historique depuis Notion :")
if st.button("Charger les données Notion"):
    with st.spinner("Chargement des données depuis Notion en cours..."):
        # Charger Recettes
        df_recettes = get_recettes_data()
        st.session_state['df_recettes'] = df_recettes

        # Charger Ingrédients (stock)
        df_ingredients = get_ingredients_data()
        st.session_state['df_ingredients'] = df_ingredients

        # Charger Ingrédients par Recette (relations)
        df_ingredients_recettes = get_ingredients_recettes_data()
        st.session_state['df_ingredients_recettes'] = df_ingredients_recettes

        # Charger l'historique des menus (pour MenuGenerator)
        df_menus_hist = get_existing_menus_data()
        st.session_state['df_menus_hist'] = df_menus_hist

        # Afficher l'état du chargement
        if not df_recettes.empty and not df_ingredients.empty and not df_ingredients_recettes.empty:
            st.success("Données Notion (Recettes, Ingrédients, Ingrédients Recettes, Historique Menus) chargées avec succès !")
            st.write(f"- Recettes: {len(df_recettes)} entrées")
            st.write(f"- Ingrédients (stock): {len(df_ingredients)} entrées")
            st.write(f"- Ingrédients par Recette: {len(df_ingredients_recettes)} entrées")
            st.write(f"- Historique des Menus: {len(df_menus_hist)} entrées")
        else:
            st.error("Certaines données Notion n'ont pas pu être chargées. Veuillez vérifier vos IDs de bases de données et les permissions Notion.")


# --- Section de Génération des Menus ---
st.header("2. Générer les menus et listes de courses")

# Vérifier si tous les DataFrames nécessaires sont chargés en session_state
fichiers_charges = (
    'df_planning' in st.session_state and st.session_state.df_planning is not None and not st.session_state.df_planning.empty and
    'df_recettes' in st.session_state and st.session_state.df_recettes is not None and not st.session_state.df_recettes.empty and
    'df_ingredients' in st.session_state and st.session_state.df_ingredients is not None and not st.session_state.df_ingredients.empty and
    'df_ingredients_recettes' in st.session_state and st.session_state.df_ingredients_recettes is not None and not st.session_state.df_ingredients_recettes.empty and
    'df_menus_hist' in st.session_state and st.session_state.df_menus_hist is not None
) # df_menus_hist peut être vide si aucun historique

if fichiers_charges:
    # Options de génération
    st.subheader("Options de Génération :")
    transportable_req = st.checkbox("Inclure uniquement les plats transportables pour les week-ends ?", value=False, help="Si coché, la génération priorisera les recettes marquées comme 'Transportable' pour les repas du week-end.")
    temps_req = st.slider("Temps de préparation maximal souhaité (en minutes) :", min_value=15, max_value=240, value=90, step=15, help="Temps maximum pour les recettes sélectionnées.")
    # nutrition_req = st.selectbox("Préférence nutritionnelle (non implémenté):", ["Aucune"], index=0) # Exemple pour future implémentation

    if st.button("Générer les Menus et Listes"):
        with st.spinner("Génération des menus et listes en cours..."):
            # Appeler MenuGenerator avec les dataframes de session_state et les options
            menu_generator = MenuGenerator(
                st.session_state.df_menus_hist,
                st.session_state.df_recettes,
                st.session_state.df_planning, # Utilise le planning chargé localement
                st.session_state.df_ingredients,
                st.session_state.df_ingredients_recettes
            )
            df_menu_genere, ingredients_utilises_menu, ingredients_stock_non_utilises, liste_courses = menu_generator.generer_menu(
                transportable_req=transportable_req, temps_req=temps_req
            )
            st.session_state['df_menus_genere_pour_notion'] = df_menu_genere # Stocker pour l'intégration Notion

            if not df_menu_genere.empty:
                st.subheader("🗓️ Aperçu des Menus Générés :")
                st.dataframe(df_menu_genere[['Date', 'Participant(s)', 'Nom Menu', 'Remarques', 'Temps Préparation']])

                # Téléchargement CSV du menu généré
                csv_buffer = io.StringIO()
                df_menu_genere.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
                csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

                st.download_button(
                    label="Télécharger Menus_generes.csv",
                    data=csv_bytes,
                    file_name=FICHIER_SORTIE_MENU_CSV,
                    mime="text/csv",
                    help="Télécharge le planning de menu généré au format CSV."
                )
                logger.info(f"Fichier CSV '{FICHIER_SORTIE_MENU_CSV}' prêt au téléchargement.")

                # Affichage et Téléchargement du récapitulatif des ingrédients
                st.subheader("🛒 Récapitulatif des Ingrédients :")
                contenu_fichier_recap_txt = []

                if ingredients_utilises_menu:
                    st.markdown("**Ingrédients en stock utilisés :**")
                    for nom_ing in ingredients_utilises_menu:
                        st.markdown(f"- {nom_ing}")
                        contenu_fichier_recap_txt.append(f"Ingrédients en stock utilisés: - {nom_ing}\n")
                else:
                    st.info("Aucun ingrédient du stock n'a été effectivement utilisé pour ce menu.")
                    contenu_fichier_recap_txt.append("Aucun ingrédient du stock n'a été effectivement utilisé pour ce menu.\n")

                if ingredients_stock_non_utilises:
                    st.markdown("**Ingrédients encore en stock (non utilisés dans ce menu) :**")
                    for nom_ing in ingredients_stock_non_utilises:
                        st.markdown(f"- {nom_ing}")
                        contenu_fichier_recap_txt.append(f"Ingrédients encore en stock (non utilisés): - {nom_ing}\n")
                else:
                    st.info("Tous les ingrédients en stock ont été utilisés ou aucun ingrédient avec Qté > 0 n'est resté.")
                    contenu_fichier_recap_txt.append("Tous les ingrédients en stock ont été utilisés ou aucun ingrédient avec Qté > 0 n'est resté.\n")

                if liste_courses:
                    st.markdown("**Liste de courses (ingrédients à acheter) :**")
                    for item_course in liste_courses:
                        st.markdown(f"- {item_course}")
                        contenu_fichier_recap_txt.append(f"Liste de courses: - {item_course}\n")
                else:
                    st.success("Aucun ingrédient à acheter pour ce menu (tout est en stock ou aucune recette planifiée).")
                    contenu_fichier_recap_txt.append("Aucun ingrédient à acheter pour ce menu.\n")
                
                txt_buffer = io.StringIO()
                txt_buffer.writelines(contenu_fichier_recap_txt)
                txt_bytes = txt_buffer.getvalue().encode("utf-8-sig")

                st.download_button(
                    label="Télécharger Listes_ingredients.txt",
                    data=txt_bytes,
                    file_name=FICHIER_SORTIE_LISTES_TXT,
                    mime="text/plain",
                    help="Télécharge le récapitulatif des ingrédients (utilisés, non utilisés, liste de courses)."
                )
                logger.info(f"Récapitulatif des ingrédients '{FICHIER_SORTIE_LISTES_TXT}' prêt au téléchargement.")

                st.success("🎉 Génération des menus et listes terminée avec succès !")

            else:
                st.warning("Aucun menu n'a pu être généré. Veuillez vérifier vos données de planification, vos recettes et vos options de génération.")
else:
    st.info("Veuillez charger le fichier Planning.csv et les données Notion à l'étape 1 pour activer la génération des menus.")


# --- Section d'intégration des menus générés vers Notion ---
st.header("3. Intégrer les menus générés à Notion")
if 'df_menus_genere_pour_notion' in st.session_state and not st.session_state.df_menus_genere_pour_notion.empty:
    st.markdown("Les menus générés sont prêts à être envoyés à votre base de données Notion 'Menus'.")
    if st.button("Envoyer les menus générés à Notion"):
        with st.spinner("Intégration des menus dans Notion en cours..."):
            integrate_with_notion(st.session_state.df_menus_genere_pour_notion, DATABASE_ID_MENUS)
            st.success("Processus d'intégration Notion terminé.")
else:
    st.info("Aucun menu généré pour l'intégration Notion. Veuillez d'abord générer des menus.")


# --- Section d'extraction des Menus existants depuis Notion (original de Generateur (4).py) ---
st.header("4. Extraire les Menus existants depuis Notion")
st.markdown("Cette section vous permet de télécharger un fichier CSV contenant les menus actuellement enregistrés dans votre base de données Notion (Base de données 'Menus').")

if st.button("Extraire et Télécharger l'historique des Menus de Notion"):
    with st.spinner("Extraction en cours depuis Notion..."):
        csv_data_extracted = get_existing_menus_data() # Réutilisation de la fonction existante
        if csv_data_extracted is not None and not csv_data_extracted.empty:
            csv_buffer = io.StringIO()
            # Convertir les dates au format YYYY-MM-DD HH:MM pour l'export CSV si ce n'est pas déjà fait
            if COLONNE_DATE_PLANNING in csv_data_extracted.columns:
                csv_data_extracted[COLONNE_DATE_PLANNING] = pd.to_datetime(csv_data_extracted[COLONNE_DATE_PLANNING], errors='coerce').dt.strftime('%Y-%m-%d %H:%M')

            csv_data_extracted.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
            csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

            st.download_button(
                label="Télécharger Menus_extraits_Notion.csv",
                data=csv_bytes,
                file_name=FICHIER_EXPORT_NOTION_CSV,
                mime="text/csv",
            )
            st.success("Fichier d'extraction Notion prêt au téléchargement.")
        else:
            st.error("L'extraction des menus existants depuis Notion a échoué ou n'a retourné aucune donnée.")


st.info("💡 N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud pour que l'application fonctionne correctement.")
