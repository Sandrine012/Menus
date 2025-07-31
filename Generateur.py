import streamlit as st
import pandas as pd
import logging
import time
import httpx
import io
import json
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError
from datetime import datetime, timedelta # timedelta est nécessaire pour la logique de génération

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes pour l'extraction de recettes et menus ---
SAISON_FILTRE = "Printemps" # Peut être rendu configurable via un widget Streamlit si désiré
NUM_ROWS_TO_EXTRACT = 100000 # Augmenté pour l'extraction des menus
BATCH_SIZE = 50
MAX_RETRIES = 7
RETRY_DELAY_INITIAL = 10
API_TIMEOUT_SECONDS = 180

# --- Constantes globales pour la génération de menus (tirées de generation.py) ---
NB_JOURS_ANTI_REPETITION = 42
VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700

# --- Noms de colonnes (tirées de generation.py, ajustées pour les IDs Notion) ---
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID" # Utilisé comme ID pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID" # Utilisé comme ID pour Ingredients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"
COLONNE_REPAS_TYPE = "Type_repas" # Assurez-vous que cette colonne existe dans votre planning
COLONNE_NB_PERSONNES = "Participant(s)" # Assurez-vous que cette colonne existe dans votre planning
COLONNE_DATE = "Date" # Assurez-vous que cette colonne existe dans votre planning


# --- Noms de fichiers pour l'exportation (constants pour Streamlit) ---
FICHIER_EXPORT_MENU_CSV = "Menus_generes.csv"
FICHIER_EXPORT_LISTE_TXT = "Liste_ingredients.txt"
FICHIER_EXPORT_NOTION_CSV = "Menus_extraits_Notion.csv"


# --- Connexion à Notion et IDs des bases de données ---
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"] # ID pour la base de données de planning des menus
    notion = Client(auth=NOTION_API_KEY, timeout_ms=API_TIMEOUT_SECONDS * 1000)
except KeyError as e:
    st.error(f"Erreur de configuration : Le secret Streamlit '{e}' est manquant. "
             f"Veuillez configurer vos clés API Notion et IDs de base de données.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas configurés
except Exception as e:
    st.error(f"Erreur lors de l'initialisation du client Notion : {e}")
    st.stop()


# --- Fonctions d'extraction de propriétés Notion ---
def extract_recette_property_value(prop_data, notion_prop_name_for_log, expected_format_key=None):
    """
    Extrait la valeur d'une propriété de recette Notion.
    """
    if not prop_data:
        return None

    prop_type = prop_data.get('type')
    if prop_type == 'title':
        return prop_data['title'][0]['plain_text'] if prop_data['title'] else None
    elif prop_type == 'rollup':
        rollup_type = prop_data['rollup'].get('type')
        if rollup_type == 'array' and prop_data['rollup']['array']:
            first_element = prop_data['rollup']['array'][0]
            if first_element and expected_format_key and expected_format_key in first_element:
                if first_element[expected_format_key] and isinstance(first_element[expected_format_key], list):
                    return ', '.join([item['plain_text'] for item in first_element[expected_format_key] if 'plain_text' in item])
                elif first_element[expected_format_key] and isinstance(first_element[expected_format_key], (int, float)):
                    return first_element[expected_format_key]
                elif first_element[expected_format_key] and 'name' in first_element[expected_format_key]:
                    return first_element[expected_format_key]['name']
                elif first_element[expected_format_key] and 'plain_text' in first_element[expected_format_key]:
                    return first_element[expected_format_key]['plain_text']
        elif rollup_type == 'number':
            return prop_data['rollup'].get('number')
        elif rollup_type == 'formula' and 'formula' in prop_data['rollup']:
            formula_type = prop_data['rollup']['formula'].get('type')
            if formula_type == 'string':
                return prop_data['rollup']['formula'].get('string')
            elif formula_type == 'number':
                return prop_data['rollup']['formula'].get('number')
            elif formula_type == 'boolean':
                return prop_data['rollup']['formula'].get('boolean')
    elif prop_type == 'unique_id':
        return prop_data['unique_id']['number']
    elif prop_type == 'rich_text':
        return prop_data['rich_text'][0]['plain_text'] if prop_data['rich_text'] else None
    elif prop_type == 'multi_select':
        return ', '.join([item['name'] for item in prop_data['multi_select']]) if prop_data['multi_select'] else None
    elif prop_type == 'number':
        return prop_data['number']
    elif prop_type == 'formula' and 'formula' in prop_data:
        formula_type = prop_data['formula'].get('type')
        if formula_type == 'string':
            return prop_data['formula'].get('string')
        elif formula_type == 'number':
            return prop_data['formula'].get('number')
        elif formula_type == 'boolean':
            return prop_data['formula'].get('boolean')
    elif prop_type == 'checkbox':
        return prop_data['checkbox']
    elif prop_type == 'relation':
        return [r['id'] for r in prop_data['relation']] if prop_data['relation'] else None
    elif prop_type == 'select':
        return prop_data['select']['name'] if prop_data['select'] else None
    elif prop_type == 'date':
        return prop_data['date']['start'] if prop_data['date'] else None
    else:
        logger.debug(f"Type de propriété non géré pour '{notion_prop_name_for_log}': {prop_type}")
    return None

def extract_property_value_generic(prop):
    """Extrait la valeur générique d'une propriété Notion."""
    if not prop:
        return None
    prop_type = prop.get('type')
    if prop_type == 'title':
        return prop['title'][0]['plain_text'] if prop['title'] else ''
    elif prop_type == 'rich_text':
        return prop['rich_text'][0]['plain_text'] if prop['rich_text'] else ''
    elif prop_type == 'number':
        return prop['number']
    elif prop_type == 'select':
        return prop['select']['name'] if prop['select'] else None
    elif prop_type == 'multi_select':
        return ', '.join([item['name'] for item in prop['multi_select']])
    elif prop_type == 'checkbox':
        return prop['checkbox']
    elif prop_type == 'url':
        return prop['url']
    elif prop_type == 'email':
        return prop['email']
    elif prop_type == 'phone_number':
        return prop['phone_number']
    elif prop_type == 'date':
        return prop['date']['start'] if prop['date'] else None
    elif prop_type == 'files':
        return [f['name'] for f in prop['files']]
    elif prop_type == 'relation':
        return [r['id'] for r in prop['relation']] if prop['relation'] else []
    elif prop_type == 'formula' and 'formula' in prop:
        formula_type = prop['formula'].get('type')
        if formula_type == 'string':
            return prop['formula'].get('string')
        elif formula_type == 'number':
            return prop['formula'].get('number')
        elif formula_type == 'boolean':
            return prop['formula'].get('boolean')
        elif formula_type == 'date':
            return prop['formula']['date'].get('start') if prop['formula'].get('date') else None
    elif prop_type == 'rollup' and 'rollup' in prop:
        rollup_data = prop['rollup']
        rollup_type = rollup_data.get('type')
        if rollup_type == 'number':
            return rollup_data.get('number')
        elif rollup_type == 'date':
            return rollup_data['date'].get('start') if rollup_data.get('date') else None
        elif rollup_type == 'array':
            # Handle array of rich_text or other types
            if rollup_data['array'] and 'plain_text' in rollup_data['array'][0]:
                return ', '.join([item['plain_text'] for item in rollup_data['array'] if 'plain_text' in item])
            elif rollup_data['array'] and 'name' in rollup_data['array'][0]: # For multi-select rollups
                return ', '.join([item['name'] for item in rollup_data['array'] if 'name' in item])
            elif rollup_data['array'] and 'title' in rollup_data['array'][0]: # For relation rollups
                return ', '.join([item['title'][0]['plain_text'] for item in rollup_data['array'] if 'title' in item and item['title']])
            elif rollup_data['array'] and 'number' in rollup_data['array'][0]:
                 return [item['number'] for item in rollup_data['array'] if 'number' in item]

    return None

# --- Fonctions de fetching de données (avec Streamlit Caching) ---
@st.cache_data(ttl=3600) # Cache les données pendant 1 heure
def fetch_notion_data(database_id, filter_json=None, columns_mapping=None, is_recettes=False):
    """
    Récupère toutes les données d'une base de données Notion, gère la pagination et le cache.
    Permet de mapper les colonnes Notion à des noms de colonnes pandas.
    """
    all_results = []
    start_cursor = None
    retry_count = 0

    while retry_count < MAX_RETRIES:
        try:
            query_params = {
                "database_id": database_id,
                "page_size": BATCH_SIZE
            }
            if start_cursor:
                query_params["start_cursor"] = start_cursor
            if filter_json:
                query_params["filter"] = filter_json

            response = notion.databases.query(**query_params)
            all_results.extend(response['results'])
            
            if response['has_more']:
                start_cursor = response['next_cursor']
            else:
                break # Toutes les pages ont été récupérées
            retry_count = 0 # Réinitialiser le compteur de tentatives en cas de succès

        except RequestTimeoutError:
            logger.warning(f"Timeout lors de la récupération des données de {database_id}. Tentative {retry_count + 1}/{MAX_RETRIES}. Attente de {RETRY_DELAY_INITIAL * (retry_count + 1)}s.")
            time.sleep(RETRY_DELAY_INITIAL * (retry_count + 1))
            retry_count += 1
        except APIResponseError as e:
            logger.error(f"Erreur API Notion pour {database_id}: {e}. Tentative {retry_count + 1}/{MAX_RETRIES}.")
            time.sleep(RETRY_DELAY_INITIAL * (retry_count + 1))
            retry_count += 1
        except httpx.ConnectError as e:
            logger.error(f"Erreur de connexion HTTP pour {database_id}: {e}. Vérifiez votre connexion internet. Tentative {retry_count + 1}/{MAX_RETRIES}.")
            time.sleep(RETRY_DELAY_INITIAL * (retry_count + 1))
            retry_count += 1
        except Exception as e:
            logger.error(f"Erreur inattendue lors de la récupération des données de {database_id}: {e}")
            retry_count += 1
            time.sleep(RETRY_DELAY_INITIAL * (retry_count + 1))
    
    if retry_count == MAX_RETRIES:
        logger.error(f"Échec de la récupération des données de {database_id} après {MAX_RETRIES} tentatives.")
        return pd.DataFrame() # Retourne un DataFrame vide en cas d'échec persistant


    data = []
    for s_page in all_results:
        row = {"Page_ID": s_page["id"]} # Toujours inclure l'ID de la page
        properties = s_page['properties']
        for notion_prop_name, column_name in columns_mapping.items() if columns_mapping else properties.items():
            prop_data = properties.get(notion_prop_name)
            if prop_data:
                if is_recettes:
                    # Pour les recettes, nous avons besoin d'extraire la bonne clé si c'est un rollup (ex: Temps_total, Aime_pas_princip)
                    if notion_prop_name == "Temps_total":
                        row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "number")
                    elif notion_prop_name == "Aime_pas_princip":
                        row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "multi_select")
                    elif notion_prop_name == "Nom":
                         row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "title")
                    elif notion_prop_name == "Saison":
                        row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "multi_select")
                    elif notion_prop_name == "Transportable":
                        row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "checkbox")
                    elif notion_prop_name == "Type_plat":
                        row[column_name] = extract_recette_property_value(prop_data, notion_prop_name, "select")
                    else:
                        row[column_name] = extract_property_value_generic(prop_data) # Fallback générique
                else:
                    row[column_name] = extract_property_value_generic(prop_data)
        data.append(row)

    df = pd.DataFrame(data)
    if COLONNE_ID_RECETTE in df.columns:
        df = df.set_index(COLONNE_ID_RECETTE, drop=False) # Important pour les lookups par ID
    return df

@st.cache_data(ttl=3600)
def get_ingredients_data():
    filter_json = {
        "property": "Type de stock",
        "select": {
            "equals": "Autre type"
        }
    }
    columns_mapping = {
        "Nom": "Nom",
        "Unité": "unité", # Assurez-vous que le nom de la propriété dans Notion est "Unité"
        "Qte reste": "Qte reste" # Assurez-vous que le nom de la propriété dans Notion est "Qte reste"
    }
    logger.info("Chargement des ingrédients depuis Notion...")
    return fetch_notion_data(DATABASE_ID_INGREDIENTS, filter_json=filter_json, columns_mapping=columns_mapping)

@st.cache_data(ttl=3600)
def get_ingredients_recettes_data():
    columns_mapping = {
        "Ingrédient ok": "Ingrédient ok", # Relation to Ingredients database, ID will be returned
        "Qté/pers_s": "Qté/pers_s",
        "Recette": "Recette_ID" # Relation to Recettes database, ID will be returned
    }
    logger.info("Chargement des ingrédients recettes depuis Notion...")
    df = fetch_notion_data(DATABASE_ID_INGREDIENTS_RECETTES, columns_mapping=columns_mapping)
    # Renommer la colonne 'Recette_ID' en COLONNE_ID_RECETTE pour match la logique de generation.py
    if 'Recette_ID' in df.columns:
        df.rename(columns={'Recette_ID': COLONNE_ID_RECETTE}, inplace=True)
    return df

@st.cache_data(ttl=3600)
def get_recettes_data():
    # Filtre pour exclure les recettes enfant et inclure la saison ou toute l'année
    filter_json = {
        "and": [
            {
                "property": "Enfant ?",
                "checkbox": {
                    "equals": False
                }
            },
            {
                "or": [
                    {
                        "property": "Saison",
                        "multi_select": {
                            "contains": SAISON_FILTRE
                        }
                    },
                    {
                        "property": "Toute l'année",
                        "checkbox": {
                            "equals": True
                        }
                    }
                ]
            },
            {
                "property": "Type_plat",
                "select": {
                    "one_of": ["Salade", "Soupe", "Plat"]
                }
            }
        ]
    }
    columns_mapping = {
        "Nom": "Nom",
        "Temps_total": "Temps_total", # Rollup de nombre
        "Aime_pas_princip": "Aime_pas_princip", # Rollup de multi-select (relation)
        "Transportable": "Transportable", # Checkbox
        "Saison": "Saison", # Multi-select
        "Type_plat": "Type_plat" # Select
    }
    logger.info("Chargement des recettes depuis Notion...")
    return fetch_notion_data(DATABASE_ID_RECETTES, filter_json=filter_json, columns_mapping=columns_mapping, is_recettes=True)

@st.cache_data(ttl=3600)
def get_existing_menus_data():
    # Récupère l'historique des menus avec la date et le nom de la recette liée
    filter_json = {
        "property": "Recette", # Nom de la propriété relation dans votre BDD Menus
        "relation": {
            "is_not_empty": True
        }
    }
    columns_mapping = {
        "Date": "Date",
        "Recette": "Recette_ID_Relation", # Relation, on obtiendra les IDs des recettes
        "Repas": "Repas",
        "Participant(s)": "Participant(s)"
    }
    logger.info("Chargement des menus existants depuis Notion...")
    df_menus = fetch_notion_data(DATABASE_ID_MENUS, filter_json=filter_json, columns_mapping=columns_mapping)

    if not df_menus.empty and 'Recette_ID_Relation' in df_menus.columns:
        # Fetch recette names for the relation IDs
        df_recettes = get_recettes_data() # Use cached recettes data

        # Ensure 'Recette_ID_Relation' contains lists of IDs
        df_menus['Recette_ID_Relation'] = df_menus['Recette_ID_Relation'].apply(lambda x: x if isinstance(x, list) else [x] if pd.notna(x) else [])

        # Explode the list of relation IDs so each ID gets its own row
        df_menus_exploded = df_menus.explode('Recette_ID_Relation')

        # Map relation IDs to recipe names
        # Ensure Page_ID in df_recettes is string for correct mapping
        df_recettes[COLONNE_ID_RECETTE] = df_recettes[COLONNE_ID_RECETTE].astype(str)
        
        # Merge to get recipe names
        df_menus_with_names = pd.merge(
            df_menus_exploded,
            df_recettes[[COLONNE_ID_RECETTE, COLONNE_NOM]],
            left_on='Recette_ID_Relation',
            right_on=COLONNE_ID_RECETTE,
            how='left'
        )
        df_menus_with_names.rename(columns={COLONNE_NOM: "Recette"}, inplace=True)
        
        # Add 'Semaine' column based on 'Date'
        if COLONNE_DATE in df_menus_with_names.columns:
            df_menus_with_names[COLONNE_DATE] = pd.to_datetime(df_menus_with_names[COLONNE_DATE], errors='coerce')
            df_menus_with_names['Semaine'] = df_menus_with_names[COLONNE_DATE].dt.isocalendar().week.astype(int)
        
        # Select and reorder relevant columns
        final_columns = ["Date", "Semaine", "Repas", "Recette", "Participant(s)"]
        return df_menus_with_names[final_columns].dropna(subset=["Recette"])
    return pd.DataFrame(columns=["Date", "Semaine", "Repas", "Recette", "Participant(s)"])


# --- Classes de génération de menus (adaptées de generation.py) ---

def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}")

class RecetteManager:
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        if COLONNE_ID_RECETTE in self.df_recettes.columns and not self.df_recettes.index.name == COLONNE_ID_RECETTE:
            self.df_recettes = self.df_recettes.set_index(COLONNE_ID_RECETTE, drop=False)

        self.df_ingredients_initial = df_ingredients.copy()
        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        self.stock_simule = self.df_ingredients_initial.copy()
        if "Qte reste" in self.stock_simule.columns:
            self.stock_simule["Qte reste"] = pd.to_numeric(self.stock_simule["Qte reste"], errors='coerce').fillna(0).astype(float)
        else:
            logger.error("'Qte reste' manquante dans df_ingredients pour stock_simule. Initialisation à 0.0.")
            self.stock_simule["Qte reste"] = 0.0

        # Assurer que les colonnes 'Page_ID' sont de type chaîne pour les lookups
        self.df_recettes[COLONNE_ID_RECETTE] = self.df_recettes[COLONNE_ID_RECETTE].astype(str)
        self.df_ingredients_recettes[COLONNE_ID_RECETTE] = self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str)
        self.df_ingredients_recettes["Ingrédient ok"] = self.df_ingredients_recettes["Ingrédient ok"].astype(str)
        self.stock_simule[COLONNE_ID_INGREDIENT] = self.stock_simule[COLONNE_ID_INGREDIENT].astype(str)
        self.df_ingredients_initial[COLONNE_ID_INGREDIENT] = self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str)

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

    def get_ingredients_for_recipe(self, recette_id_str):
        try:
            recette_id_str = str(recette_id_str)
            ingredients = self.df_ingredients_recettes[
                self.df_ingredients_recettes[COLONNE_ID_RECETTE] == recette_id_str
            ][["Ingrédient ok", "Qté/pers_s"]].to_dict('records')
            if not ingredients:
                logger.debug(f"Aucun ingrédient trouvé pour recette {recette_id_str} dans df_ingredients_recettes")
            return ingredients
        except Exception as e:
            logger.error(f"Erreur récupération ingrédients pour {recette_id_str} : {e}")
            return []

    def _trouver_ingredients_stock_eleve(self):
        seuil_gr = 100
        seuil_pc = 1
        ingredients_stock = {}
        if not all(col in self.stock_simule.columns for col in ["Qte reste", "unité", COLONNE_ID_INGREDIENT, "Nom"]):
            logger.warning("Colonnes manquantes dans stock_simule pour _trouver_ingredients_stock_eleve. Retourne {}")
            return {}

        for _, row in self.stock_simule.iterrows():
            try:
                qte = float(str(row["Qte reste"]).replace(",", "."))
                unite = str(row["unité"]).lower()
                page_id = str(row[COLONNE_ID_INGREDIENT])
                if (unite in ["gr", "g", "ml", "cl"] and qte >= seuil_gr) or \
                   (unite in ["pc", "tranches"] and qte >= seuil_pc):
                    ingredients_stock[page_id] = row["Nom"]
            except (ValueError, KeyError) as e:
                logger.debug(f"Erreur dans _trouver_ingredients_stock_eleve pour ligne {row.get('Nom', 'ID inconnu')}: {e}")
                continue
        return ingredients_stock

    def recette_utilise_ingredient_anti_gaspi(self, recette_id_str):
        try:
            ingredients = self.get_ingredients_for_recipe(recette_id_str)
            return any(str(ing.get("Ingrédient ok")) in self.anti_gaspi_ingredients for ing in ingredients if ing.get("Ingrédient ok"))
        except Exception as e:
            logger.error(f"Erreur dans recette_utilise_ingredient_anti_gaspi pour {recette_id_str}: {e}")
            return False

    def calculer_quantite_necessaire(self, recette_id_str, nb_personnes):
        ingredients_necessaires = {}
        try:
            ingredients_recette = self.get_ingredients_for_recipe(recette_id_str)
            if not ingredients_recette: return {}

            for ing in ingredients_recette:
                try:
                    ing_id = str(ing.get("Ingrédient ok"))
                    if not ing_id or ing_id.lower() in ['nan', 'none', '']: continue

                    qte_str = str(ing.get("Qté/pers_s", "0")).replace(',', '.')
                    qte_par_personne = float(qte_str)
                    ingredients_necessaires[ing_id] = qte_par_personne * nb_personnes
                except (ValueError, TypeError, KeyError) as e:
                    logger.debug(f"Erreur calcul quantité ingrédient {ing.get('Ingrédient ok')} pour recette {recette_id_str}: {e}. Qté str: '{ing.get('Qté/pers_s')}'")
                    continue
            return ingredients_necessaires
        except Exception as e:
            logger.error(f"Erreur globale calculer_quantite_necessaire pour {recette_id_str}: {e}")
            return {}

    def evaluer_disponibilite_et_manquants(self, recette_id_str, nb_personnes):
        """
        Évalue la disponibilité et identifie les quantités manquantes pour une recette.
        Retourne: score_moyen_dispo, pourcentage_dispo, dict_ingredients_manquants {ing_id: qte_manquante}
        """
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        if not ingredients_necessaires: return 0, 0, {}

        total_ingredients_definis = len(ingredients_necessaires)
        ingredients_disponibles_compteur = 0
        score_total_dispo = 0
        ingredients_manquants = {}

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            ing_stock_df = self.stock_simule[self.stock_simule[COLONNE_ID_INGREDIENT] == ing_id_str]

            qte_en_stock = 0.0
            if not ing_stock_df.empty:
                try:
                    qte_en_stock = float(ing_stock_df["Qte reste"].iloc[0])
                except (ValueError, IndexError, KeyError) as e:
                    logger.error(f"Erreur lecture stock pour {ing_id_str} rec {recette_id_str}: {e}")
            else:
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule.")


            ratio_dispo = 0.0
            if qte_necessaire > 0:
                ratio_dispo = min(1.0, qte_en_stock / qte_necessaire)

            if ratio_dispo >= 0.3: ingredients_disponibles_compteur += 1
            score_total_dispo += ratio_dispo

            if qte_en_stock < qte_necessaire:
                quantite_manquante = qte_necessaire - qte_en_stock
                if quantite_manquante > 0:
                    ingredients_manquants[ing_id_str] = quantite_manquante

            logger.debug(f"Ingr {ing_id_str} rec {recette_id_str}: stock={qte_en_stock}, nec={qte_necessaire}, ratio={ratio_dispo:.2f}, manquant={ingredients_manquants.get(ing_id_str, 0):.2f}")

        pourcentage_dispo = (ingredients_disponibles_compteur / total_ingredients_definis) * 100 if total_ingredients_definis > 0 else 0
        score_moyen_dispo = score_total_dispo / total_ingredients_definis if total_ingredients_definis > 0 else 0

        logger.debug(f"Éval recette {recette_id_str}: Score={score_moyen_dispo:.2f}, %Dispo={pourcentage_dispo:.0f}%")
        return score_moyen_dispo, pourcentage_dispo, ingredients_manquants

    def decrementer_stock(self, recette_id_str, nb_personnes):
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        ingredients_consommes_ids = set()

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            idx_list = self.stock_simule.index[self.stock_simule[COLONNE_ID_INGREDIENT] == ing_id_str].tolist()
            if not idx_list:
                logger.debug(f"Ingrédient {ing_id_str} (recette {recette_id_str}) non trouvé dans stock_simule pour décrémentation.")
                continue
            idx = idx_list[0]

            try:
                qte_actuelle = float(self.stock_simule.loc[idx, "Qte reste"])
                if qte_actuelle > 0 and qte_necessaire > 0:
                    qte_a_consommer = min(qte_actuelle, qte_necessaire)
                    nouvelle_qte = qte_actuelle - qte_a_consommer
                    self.stock_simule.loc[idx, "Qte reste"] = nouvelle_qte

                    if qte_a_consommer > 0:
                        ingredients_consommes_ids.add(ing_id_str)
                        logger.debug(f"Stock décrémenté pour {ing_id_str} (recette {recette_id_str}): {qte_actuelle:.2f} -> {nouvelle_qte:.2f} (consommé: {qte_a_consommer:.2f})")
            except (ValueError, KeyError) as e:
                logger.error(f"Erreur décrémentation stock pour {ing_id_str} (recette {recette_id_str}): {e}")

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()
        return list(ingredients_consommes_ids)

    def obtenir_nom(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                return self.df_recettes.loc[recette_page_id_str, COLONNE_NOM]
            else:
                return self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == recette_page_id_str][COLONNE_NOM].iloc[0]
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvé dans df_recettes (obtenir_nom).")
            return f"Recette_ID_{recette_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom pour recette ID {recette_page_id_str}: {e}")
            return None

    def obtenir_nom_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            # Chercher dans le df_ingredients_initial qui contient les noms originaux
            nom = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT] == ing_page_id_str, 'Nom'].iloc[0]
            return nom
        except (IndexError, KeyError):
            logger.warning(f"Nom introuvable pour ingrédient ID: {ing_page_id_str} dans df_ingredients_initial.")
            return f"ID_Ing_{ing_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom_ingredient_par_id pour {ing_page_id_str}: {e}")
            return None

    def est_adaptee_aux_participants(self, recette_page_id_str, participants_str_codes):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                recette_info = self.df_recettes.loc[recette_page_id_str]
            else:
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == recette_page_id_str].iloc[0]

            if COLONNE_AIME_PAS_PRINCIP not in recette_info or pd.isna(recette_info[COLONNE_AIME_PAS_PRINCIP]):
                return True
            n_aime_pas = [code.strip() for code in str(recette_info[COLONNE_AIME_PAS_PRINCIP]).split(",") if code.strip()]
            participants_actifs = [code.strip() for code in participants_str_codes.split(",") if code.strip()]
            return not any(code_participant in n_aime_pas for code_participant in participants_actifs)
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvée pour vérifier adaptation participants.")
            return True
        except Exception as e:
            logger.error(f"Erreur vérification adaptation participants pour {recette_page_id_str}: {e}")
            return False

    def est_transportable(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                valeur = str(self.df_recettes.loc[recette_page_id_str, "Transportable"]).strip().lower()
            else:
                valeur = str(self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == recette_page_id_str]["Transportable"].iloc[0]).strip().lower()
            return valeur == "oui"
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour transportable.")
            return False
        except Exception as e:
            logger.error(f"Erreur vérification transportable pour {recette_page_id_str}: {e}")
            return False

    def obtenir_temps_preparation(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                recette_info = self.df_recettes.loc[recette_page_id_str]
            else:
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE] == recette_page_id_str].iloc[0]

            if COLONNE_TEMPS_TOTAL in recette_info and pd.notna(recette_info[COLONNE_TEMPS_TOTAL]):
                return int(recette_info[COLONNE_TEMPS_TOTAL])
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour temps_preparation.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (ValueError, TypeError):
            logger.warning(f"Temps de prép non valide pour recette {recette_page_id_str}. Valeur par défaut.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except Exception as e:
            logger.error(f"Erreur obtention temps prép pour {recette_page_id_str}: {e}")
            return VALEUR_DEFAUT_TEMPS_PREPARATION


class MenusHistoryManager:
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()
        if "Date" in self.df_menus_historique.columns:
            self.df_menus_historique["Date"] = pd.to_datetime(
                self.df_menus_historique["Date"],
                errors="coerce"
            )
            self.df_menus_historique.dropna(subset=["Date"], inplace=True)
        else:
            logger.warning("La colonne 'Date' est manquante dans l'historique des menus. L'historique ne sera pas utilisé.")
            self.df_menus_historique = pd.DataFrame(columns=["Date", "Semaine", "Recette"]) # Crée un DF vide si la colonne Date manque


class MenuGenerator:
    def __init__(self, df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes):
        self.df_planning = df_planning.copy()
        if COLONNE_DATE in self.df_planning.columns:
            self.df_planning[COLONNE_DATE] = pd.to_datetime(self.df_planning[COLONNE_DATE], errors='coerce')
            self.df_planning.dropna(subset=[COLONNE_DATE], inplace=True)
        else:
            logger.error(f"'{COLONNE_DATE}' manquante dans le planning. Impossible de générer les menus.")
            raise ValueError(f"Colonne '{COLONNE_DATE}' manquante dans le fichier de planning.")

        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)

        self.ingredients_a_acheter_cumules = {} # Dictionnaire pour la liste de courses

    def recettes_meme_semaine_annees_precedentes(self, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in [COLONNE_DATE, 'Semaine', 'Recette']):
                return set()
            semaine_actuelle = date_actuelle.isocalendar()[1]
            annee_actuelle = date_actuelle.year
            df_menus_semaine = df_hist[
                (df_hist["Semaine"].astype(int) == semaine_actuelle) &
                (df_hist[COLONNE_DATE].dt.year < annee_actuelle) &
                pd.notna(df_hist["Recette"])
            ]
            return set(df_menus_semaine["Recette"].astype(str).unique())
        except Exception as e:
            logger.error(f"Erreur recettes_meme_semaine_annees_precedentes pour {date_actuelle}: {e}")
            return set()

    def est_recente(self, recette_page_id_str, date_actuelle):
        """
        Retourne True si la recette a déjà été planifiée entre
        date_actuelle - NB_JOURS_ANTI_REPETITION et date_actuelle.
        """
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty:
                return False

            date_limite = date_actuelle - timedelta(days=NB_JOURS_ANTI_REPETITION)

            recette_nom = self.recette_manager.obtenir_nom(recette_page_id_str)
            if not recette_nom:
                return True # Si on ne trouve pas le nom, on considère qu'elle est récente pour éviter des erreurs

            # Filtre les menus historiques pour les recettes récentes
            recent_menus = df_hist[
                (df_hist[COLONNE_DATE] >= date_limite) &
                (df_hist[COLONNE_DATE] <= date_actuelle) &
                (df_hist["Recette"].astype(str) == recette_nom)
            ]
            return not recent_menus.empty
        except Exception as e:
            logger.error(f"Erreur vérification recette récente pour {recette_page_id_str} à {date_actuelle}: {e}")
            return False


    def filtrer_recettes_par_critere(self, df_recettes_candidates, date_repas_dt, participants_str_codes, plats_transportables_semaine, repas_type, temps_max_souhaite=None, recette_specifique_id=None, anti_repetition=True, anti_gaspi_priorite=False):
        """
        Filtre et classe les recettes selon divers critères.
        Retourne une liste de Page_ID des recettes filtrées et triées.
        """
        recettes_filtrees = df_recettes_candidates.copy()

        # 1. Filtre par recette spécifique si fournie
        if recette_specifique_id:
            recettes_filtrees = recettes_filtrees[recettes_filtrees[COLONNE_ID_RECETTE].astype(str) == str(recette_specifique_id)]
            if recettes_filtrees.empty:
                logger.warning(f"Recette spécifique {recette_specifique_id} non trouvée ou déjà filtrée.")
                return pd.DataFrame() # Retourne un DF vide si la recette spécifique n'est pas trouvée

        if recettes_filtrees.empty: return pd.DataFrame()

        # 2. Filtre par type de plat (si pertinent pour ce repas)
        if repas_type == "Dej (B)" : # Pour les repas B, le type de plat doit être "Plat"
             recettes_filtrees = recettes_filtrees[recettes_filtrees["Type_plat"] == "Plat"]
        elif repas_type == "Dej (A)" or repas_type == "Soir (A+B)":
            recettes_filtrees = recettes_filtrees[recettes_filtrees["Type_plat"].isin(["Salade", "Soupe", "Plat"])]


        # 3. Filtre par saison et "Toute l'année" (déjà fait par get_recettes_data)

        # 4. Filtre par "Aime pas"
        recettes_filtrees = recettes_filtrees[
            recettes_filtrees[COLONNE_ID_RECETTE].apply(
                lambda x: self.recette_manager.est_adaptee_aux_participants(x, participants_str_codes)
            )
        ]

        if recettes_filtrees.empty:
            logger.debug(f"Pas de recette après filtre 'Aime pas' pour {date_repas_dt} {repas_type} avec {participants_str_codes}.")
            return pd.DataFrame()

        # 5. Filtre par temps de préparation maximum
        if temps_max_souhaite is not None:
            recettes_filtrees = recettes_filtrees[
                recettes_filtrees[COLONNE_ID_RECETTE].apply(
                    lambda x: self.recette_manager.obtenir_temps_preparation(x) <= temps_max_souhaite
                )
            ]

        if recettes_filtrees.empty:
            logger.debug(f"Pas de recette après filtre de temps pour {date_repas_dt} {repas_type}.")
            return pd.DataFrame()

        # 6. Filtre pour les repas transportables si c'est un repas B
        if "B" in participants_str_codes and repas_type == "Dej (B)": # Si B est présent et c'est un déjeuner pour B
            recettes_filtrees = recettes_filtrees[
                recettes_filtrees[COLONNE_ID_RECETTE].apply(self.recette_manager.est_transportable)
            ]
            # Assurez-vous que les plats transportables de la semaine ne sont pas répétés pour les repas B
            if plats_transportables_semaine:
                 recettes_filtrees = recettes_filtrees[
                     ~recettes_filtrees[COLONNE_ID_RECETTE].isin(plats_transportables_semaine)
                 ]

        if recettes_filtrees.empty:
            logger.debug(f"Pas de recette après filtre transportable/répétition B pour {date_repas_dt} {repas_type}.")
            return pd.DataFrame()


        # 7. Filtre anti-répétition
        if anti_repetition:
            recettes_filtrees = recettes_filtrees[
                ~recettes_filtrees[COLONNE_ID_RECETTE].apply(
                    lambda x: self.est_recente(x, date_repas_dt)
                )
            ]
            if recettes_filtrees.empty:
                logger.debug(f"Pas de recette après filtre anti-répétition pour {date_repas_dt} {repas_type}.")
                # Si aucune recette n'est trouvée avec l'anti-répétition, on la désactive pour cette itération
                logger.info(f"Pas de recette sans répétition pour {date_repas_dt} {repas_type}. Essai sans anti-répétition stricte.")
                recettes_filtrees = df_recettes_candidates.copy() # Reprend le DF initial
                recettes_filtrees = recettes_filtrees[
                    recettes_filtrees[COLONNE_ID_RECETTE].apply(
                        lambda x: self.recette_manager.est_adaptee_aux_participants(x, participants_str_codes)
                    )
                ]
                if temps_max_souhaite is not None:
                    recettes_filtrees = recettes_filtrees[
                        recettes_filtrees[COLONNE_ID_RECETTE].apply(
                            lambda x: self.recette_manager.obtenir_temps_preparation(x) <= temps_max_souhaite
                        )
                    ]
                if "B" in participants_str_codes and repas_type == "Dej (B)":
                    recettes_filtrees = recettes_filtrees[
                        recettes_filtrees[COLONNE_ID_RECETTE].apply(self.recette_manager.est_transportable)
                    ]
                    if plats_transportables_semaine:
                         recettes_filtrees = recettes_filtrees[
                             ~recettes_filtrees[COLONNE_ID_RECETTE].isin(plats_transportables_semaine)
                         ]


        if recettes_filtrees.empty:
            logger.warning(f"Aucune recette disponible après tous les filtres pour {date_repas_dt} {repas_type}.")
            return pd.DataFrame()

        # Évaluation de la disponibilité des ingrédients et calcul des manquants
        recettes_filtrees_avec_scores = []
        for _, row in recettes_filtrees.iterrows():
            recette_id = row[COLONNE_ID_RECETTE]
            score_dispo, pourcentage_dispo, ingredients_manquants = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id, len(participants_str_codes.split(',')))
            recettes_filtrees_avec_scores.append({
                COLONNE_ID_RECETTE: recette_id,
                COLONNE_NOM: row[COLONNE_NOM],
                "Score_Dispo": score_dispo,
                "Pourcentage_Dispo": pourcentage_dispo,
                "Ingredients_Manquants": ingredients_manquants,
                "Utilise_Anti_Gaspi": self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id),
                "Temps_Preparation": self.recette_manager.obtenir_temps_preparation(recette_id)
            })

        df_recettes_candidates_eval = pd.DataFrame(recettes_filtrees_avec_scores)

        # Tri des recettes: Priorité anti-gaspi, puis % de dispo, puis temps de préparation (plus rapide en premier)
        df_recettes_candidates_eval["Tri_Key"] = 0
        if anti_gaspi_priorite:
            # Si priorité anti-gaspi, les recettes qui utilisent le stock élevé viennent en premier
            df_recettes_candidates_eval.loc[df_recettes_candidates_eval["Utilise_Anti_Gaspi"], "Tri_Key"] = 1000

        df_recettes_candidates_eval["Tri_Key"] += df_recettes_candidates_eval["Pourcentage_Dispo"]
        df_recettes_candidates_eval["Tri_Key"] -= df_recettes_candidates_eval["Temps_Preparation"] / 100 # Moins de temps = plus haut score

        df_recettes_candidates_eval = df_recettes_candidates_eval.sort_values(by="Tri_Key", ascending=False)


        return df_recettes_candidates_eval[[COLONNE_ID_RECETTE, COLONNE_NOM, "Ingredients_Manquants"]]

    def generer_menu_repas_b(self, date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms):
        """
        Génère un plat pour un repas B.
        Priorise les plats transportables, puis les plats non récemment utilisés.
        """
        participants_str_codes = "B"
        plats_disponibles = self.recette_manager.df_recettes[
            self.recette_manager.df_recettes["Type_plat"] == "Plat"
        ].copy() # Ne considérer que les "Plat" pour le repas B

        # Tenter d'abord avec les plats transportables non encore utilisés cette semaine
        candidates_transportables = self.filtrer_recettes_par_critere(
            plats_disponibles, date_repas_dt, participants_str_codes,
            plats_transportables_semaine, "Dej (B)",
            temps_max_souhaite=TEMPS_MAX_EXPRESS, # Repas B doit être rapide
            anti_repetition=True,
            anti_gaspi_priorite=True # Prioriser l'anti-gaspi pour les repas B
        )
        candidates_transportables = candidates_transportables[
             ~candidates_transportables[COLONNE_ID_RECETTE].isin(repas_b_utilises_ids)
        ] # Exclure les repas B déjà utilisés

        if not candidates_transportables.empty:
            recette_choisie_id = candidates_transportables.iloc[0][COLONNE_ID_RECETTE]
            nom_plat = self.recette_manager.obtenir_nom(recette_choisie_id)
            remarques = "Transportable choisi"
            logger.info(f"Repas B: {nom_plat} ({recette_choisie_id}) - {remarques}")
            return nom_plat, recette_choisie_id, remarques
        else:
            logger.warning(f"Aucun plat transportable trouvé pour repas B le {date_repas_dt}. Extension de la recherche.")
            # Si pas de transportable, chercher n'importe quel plat rapide non récemment utilisé
            candidates_generales = self.filtrer_recettes_par_critere(
                plats_disponibles, date_repas_dt, participants_str_codes,
                plats_transportables_semaine, "Dej (B)",
                temps_max_souhaite=TEMPS_MAX_RAPIDE,
                anti_repetition=True,
                anti_gaspi_priorite=True
            )
            candidates_generales = candidates_generales[
                 ~candidates_generales[COLONNE_ID_RECETTE].isin(repas_b_utilises_ids)
            ]

            if not candidates_generales.empty:
                recette_choisie_id = candidates_generales.iloc[0][COLONNE_ID_RECETTE]
                nom_plat = self.recette_manager.obtenir_nom(recette_choisie_id)
                remarques = "Plat rapide non transportable choisi"
                logger.info(f"Repas B: {nom_plat} ({recette_choisie_id}) - {remarques}")
                return nom_plat, recette_choisie_id, remarques

        logger.warning(f"Aucun plat trouvé pour repas B le {date_repas_dt} après tentatives.")
        return "À définir (B)", None, "Aucun plat trouvé pour B"


    def generer_menu_repas_principal_commun(self, date_repas_dt, participants_str_codes, temps_max_souhaite, menu_recent_noms, repas_type, recette_specifique_id=None):
        """
        Génère un plat principal commun (déjeuner A ou dîner A+B).
        """
        plats_disponibles = self.recette_manager.df_recettes.copy()

        # Filtrer d'abord par type de plat Salade, Soupe ou Plat
        plats_disponibles = plats_disponibles[plats_disponibles["Type_plat"].isin(["Salade", "Soupe", "Plat"])]


        candidates = self.filtrer_recettes_par_critere(
            plats_disponibles,
            date_repas_dt,
            participants_str_codes,
            [], # Pas de plats transportables pour les repas communs
            repas_type,
            temps_max_souhaite=temps_max_souhaite,
            recette_specifique_id=recette_specifique_id,
            anti_repetition=True,
            anti_gaspi_priorite=True
        )

        if not candidates.empty:
            recette_choisie_id = candidates.iloc[0][COLONNE_ID_RECETTE]
            nom_plat = self.recette_manager.obtenir_nom(recette_choisie_id)
            ingredients_manquants = candidates.iloc[0]["Ingredients_Manquants"]
            remarques = "Plat généré"
            logger.info(f"{repas_type}: {nom_plat} ({recette_choisie_id}) - {remarques}")
            return nom_plat, recette_choisie_id, remarques, ingredients_manquants
        else:
            logger.warning(f"Aucun plat trouvé pour {repas_type} le {date_repas_dt}.")
            return f"À définir ({repas_type})", None, "Aucun plat trouvé", {}


    def generer_repas_journalier(self, row_planning, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms):
        """
        Génère les repas pour une journée donnée du planning.
        """
        date_repas_dt = row_planning[COLONNE_DATE]
        repas_type = row_planning[COLONNE_REPAS_TYPE]
        participants_str = row_planning[COLONNE_NB_PERSONNES]
        recette_specifique_planning_id = str(row_planning.get(COLONNE_ID_RECETTE, '')).strip() if COLONNE_ID_RECETTE in row_planning else None
        if recette_specifique_planning_id and recette_specifique_planning_id.lower() in ['nan', 'none', '']:
            recette_specifique_planning_id = None

        nom_plat_final = "Erreur - Plat non défini"
        recette_choisie_id = None
        remarques_repas = ""
        temps_prep_final = 0
        ingredients_consommes_ce_repas = []
        ingredients_manquants_pour_recette_choisie = {}

        logger.info(f"Génération pour {date_repas_dt.strftime('%Y-%m-%d')} - {repas_type} - Participants: {participants_str}")

        if participants_str == "B":
            nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
            )
            # Pour les repas B, les ingrédients manquants sont calculés au moment du choix
            if recette_choisie_id:
                _ , _, ingredients_manquants_pour_recette_choisie = self.recette_manager.evaluer_disponibilite_et_manquants(recette_choisie_id, 1) # Repas B = 1 personne
        elif participants_str == "A" :
            nom_plat_final, recette_choisie_id, remarques_repas, ingredients_manquants_pour_recette_choisie = self.generer_menu_repas_principal_commun(
                date_repas_dt, participants_str, TEMPS_MAX_EXPRESS, menu_recent_noms, repas_type, recette_specifique_id
            )
        elif participants_str == "A+B":
            nom_plat_final, recette_choisie_id, remarques_repas, ingredients_manquants_pour_recette_choisie = self.generer_menu_repas_principal_commun(
                date_repas_dt, participants_str, TEMPS_MAX_RAPIDE, menu_recent_noms, repas_type, recette_specifique_id
            )
        elif participants_str == "À définir":
            nom_plat_final = "À définir"
            recette_choisie_id = None
            remarques_repas = "Repas à définir manuellement"
            ingredients_manquants_pour_recette_choisie = {} # Aucun ingrédient manquant pour un repas non défini
        else:
            logger.warning(f"Type de participant inconnu: {participants_str} pour le {date_repas_dt} {repas_type}")
            nom_plat_final = f"Inconnu ({participants_str})"
            recette_choisie_id = None
            remarques_repas = "Type de participant non géré"
            ingredients_manquants_pour_recette_choisie = {}

        if recette_choisie_id:
            temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
            # Décrémenter le stock simulé
            num_persons = 1 if participants_str == "B" else (2 if participants_str in ["A", "A+B"] else 0) # Ajustez selon votre logique
            ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, num_persons)

            # Ajouter les ingrédients manquants au cumul
            for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                self.ingredients_a_acheter_cumules[ing_id] = self.ingredients_a_acheter_cumules.get(ing_id, 0) + qte_manquante


        return {
            COLONNE_DATE: date_repas_dt,
            "Semaine": date_repas_dt.isocalendar()[1],
            COLONNE_REPAS_TYPE: repas_type,
            COLONNE_NB_PERSONNES: participants_str,
            "Recette": nom_plat_final,
            "Recette_ID": recette_choisie_id, # Ajout de l'ID pour l'intégration Notion
            "Remarques": remarques_repas,
            "Temps_Préparation": temps_prep_final,
            "Ingrédients_Consommés_IDs": ingredients_consommes_ce_repas,
            "Ingrédients_Manquants_Recette": ingredients_manquants_pour_recette_choisie
        }


    def generer_menus_complet(self):
        """
        Génère le planning complet des menus.
        """
        menus_generes_liste = []
        plats_transportables_semaine = []
        repas_b_utilises_ids = []

        # Trier le planning par date
        self.df_planning = self.df_planning.sort_values(by=COLONNE_DATE).reset_index(drop=True)

        current_week = None

        for index, row in self.df_planning.iterrows():
            date_repas = row[COLONNE_DATE]
            repas_type = row[COLONNE_REPAS_TYPE]
            participants_str = row[COLONNE_NB_PERSONNES]
            recette_specifique = row.get("Recette_specifique", None) # Si une recette est déjà spécifiée dans le planning

            # Réinitialiser les plats transportables et repas B utilisés au début d'une nouvelle semaine
            if current_week is None or date_repas.isocalendar()[1] != current_week:
                current_week = date_repas.isocalendar()[1]
                plats_transportables_semaine = []
                repas_b_utilises_ids = []
                logger.info(f"Début de la semaine {current_week}.")

            # Si une recette est déjà spécifiée dans le planning, l'utiliser directement
            if pd.notna(recette_specifique) and recette_specifique.strip() != "":
                recette_id_specifique = None
                try:
                    # Tenter de trouver l'ID de la recette spécifiée par son nom
                    df_recettes_match = self.recette_manager.df_recettes[
                        self.recette_manager.df_recettes[COLONNE_NOM].astype(str).str.lower() == str(recette_specifique).strip().lower()
                    ]
                    if not df_recettes_match.empty:
                        recette_id_specifique = df_recettes_match.iloc[0][COLONNE_ID_RECETTE]
                        logger.info(f"Recette spécifiée dans le planning trouvée: {recette_specifique} (ID: {recette_id_specifique})")
                    else:
                        logger.warning(f"Recette spécifiée '{recette_specifique}' non trouvée dans la base de données des recettes. Tentative de génération.")
                except Exception as e:
                    logger.error(f"Erreur lors de la recherche de la recette spécifiée '{recette_specifique}': {e}")


                if recette_id_specifique:
                    # Simuler la sélection d'une recette spécifique
                    recette_choisie_id = recette_id_specifique
                    nom_plat_final = recette_specifique
                    remarques_repas = "Recette spécifiée dans le planning"
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    num_persons = 1 if participants_str == "B" else (2 if participants_str in ["A", "A+B"] else 0)
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, num_persons)
                    _, _, ingredients_manquants_pour_recette_choisie = self.recette_manager.evaluer_disponibilite_et_manquants(recette_choisie_id, num_persons)
                    for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                         self.ingredients_a_acheter_cumules[ing_id] = self.ingredients_a_acheter_cumules.get(ing_id, 0) + qte_manquante
                else:
                    # Si la recette spécifiée n'est pas trouvée, essayer de générer
                    logger.warning(f"Recette spécifiée '{recette_specifique}' non trouvée ou invalide. Tentative de génération automatique.")
                    generated_meal_info = self.generer_repas_journalier(row, plats_transportables_semaine, repas_b_utilises_ids, self.recettes_meme_semaine_annees_precedentes(date_repas))
                    recette_choisie_id = generated_meal_info["Recette_ID"]
                    nom_plat_final = generated_meal_info["Recette"]
                    remarques_repas = generated_meal_info["Remarques"]
                    temps_prep_final = generated_meal_info["Temps_Préparation"]
                    ingredients_consommes_ce_repas = generated_meal_info["Ingrédients_Consommés_IDs"]
                    ingredients_manquants_pour_recette_choisie = generated_meal_info["Ingrédients_Manquants_Recette"]
            else:
                # Génération normale si aucune recette n'est spécifiée
                generated_meal_info = self.generer_repas_journalier(row, plats_transportables_semaine, repas_b_utilises_ids, self.recettes_meme_semaine_annees_precedentes(date_repas))
                recette_choisie_id = generated_meal_info["Recette_ID"]
                nom_plat_final = generated_meal_info["Recette"]
                remarques_repas = generated_meal_info["Remarques"]
                temps_prep_final = generated_meal_info["Temps_Préparation"]
                ingredients_consommes_ce_repas = generated_meal_info["Ingrédients_Consommés_IDs"]
                ingredients_manquants_pour_recette_choisie = generated_meal_info["Ingrédients_Manquants_Recette"]


            if recette_choisie_id and self.recette_manager.est_transportable(recette_choisie_id):
                plats_transportables_semaine.append(recette_choisie_id)
            if participants_str == "B" and recette_choisie_id:
                repas_b_utilises_ids.append(recette_choisie_id)

            menus_generes_liste.append({
                COLONNE_DATE: date_repas.strftime('%Y-%m-%d'), # Formatage pour l'affichage/export
                "Semaine": date_repas.isocalendar()[1],
                COLONNE_REPAS_TYPE: repas_type,
                COLONNE_NB_PERSONNES: participants_str,
                "Recette": nom_plat_final,
                "Recette_ID": recette_choisie_id,
                "Temps_Préparation": temps_prep_final,
                "Remarques": remarques_repas,
                "Ingrédients_Consommés_IDs": ingredients_consommes_ce_repas,
                "Ingrédients_Manquants_Recette": ingredients_manquants_pour_recette_choisie
            })
            logger.info(f"Généré: {date_repas.strftime('%Y-%m-%d')} {repas_type}: {nom_plat_final}")

        df_menus_complet = pd.DataFrame(menus_generes_liste)
        return df_menus_complet, self.ingredients_a_acheter_cumules

# --- Fonctions de traitement de données et de génération de fichiers ---

def process_and_generate(df_planning, df_recettes, df_ingredients, df_ingredients_recettes, df_existing_menus):
    """
    Orchestre le processus de génération des menus et des listes de courses.
    Remplace la logique de process_data et generate_output_files.
    """
    logger.info("Début du processus de génération de menus...")

    # Assurer que les DataFrames nécessaires ne sont pas vides
    if df_planning.empty or df_recettes.empty or df_ingredients.empty or df_ingredients_recettes.empty:
        st.warning("Veuillez charger tous les fichiers nécessaires et les données Notion pour activer la génération.")
        return pd.DataFrame(), pd.DataFrame(), {} # Return empty DFs and dict

    # Initialiser le générateur de menus
    menu_generator = MenuGenerator(df_existing_menus, df_recettes, df_planning, df_ingredients, df_ingredients_recettes)

    # Générer les menus complets et la liste des ingrédients à acheter
    df_menus_complet, ingredients_a_acheter_cumules_ids = menu_generator.generer_menus_complet()
    
    # Préparer la liste de courses lisible
    liste_courses_data = []
    if ingredients_a_acheter_cumules_ids:
        # Assurez-vous que les IDs des ingrédients sont des chaînes pour la recherche
        df_ingredients[COLONNE_ID_INGREDIENT] = df_ingredients[COLONNE_ID_INGREDIENT].astype(str)

        for ing_id, qte_manquante in ingredients_a_acheter_cumules_ids.items():
            ing_info = df_ingredients[df_ingredients[COLONNE_ID_INGREDIENT] == str(ing_id)]
            if not ing_info.empty:
                nom_ingredient = ing_info["Nom"].iloc[0]
                unite = ing_info["unité"].iloc[0] if "unité" in ing_info.columns else ""
                
                # Formater la quantité pour l'affichage
                qte_formattee = f"{qte_manquante:.2f}".rstrip('0').rstrip('.') if qte_manquante % 1 else str(int(qte_manquante))

                liste_courses_data.append(f"- {nom_ingredient}: {qte_formattee} {unite}")
            else:
                logger.warning(f"Ingrédient ID '{ing_id}' non trouvé dans la base d'ingrédients pour la liste de courses.")
                liste_courses_data.append(f"- ID_Ing_{ing_id} (quantité manquante: {qte_manquante})")

    liste_courses_texte = "\n".join(liste_courses_data) if liste_courses_data else "Aucun ingrédient à acheter."


    # Préparer les DataFrames pour l'exportation
    df_menus_export = df_menus_complet[[COLONNE_DATE, COLONNE_NB_PERSONNES, COLONNE_REPAS_TYPE, "Recette", "Temps_Préparation", "Remarques", "Recette_ID"]]

    st.success("Génération des menus et de la liste de courses terminée!")

    return df_menus_export, liste_courses_texte, ingredients_a_acheter_cumules_ids # Retourne aussi les ingrédients pour le débogage/vérification


def get_page_id_by_name(database_id, property_name, page_name):
    """
    Recherche l'ID d'une page Notion par son nom dans une propriété spécifique.
    Ceci est une version simplifiée et pourrait nécessiter des ajustements
    selon la structure exacte de vos bases de données Notion,
    en particulier pour les relations.
    """
    try:
        filter_query = {
            "property": property_name,
            "title": { # Assumons que le nom est dans une propriété de type 'title'
                "equals": page_name
            }
        }
        # Si la propriété n'est pas un titre, il faut adapter
        # Ex: for text property: "rich_text": {"equals": page_name}

        response = notion.databases.query(
            database_id=database_id,
            filter=filter_query,
            page_size=1
        )
        if response and response['results']:
            return response['results'][0]['id']
        return None
    except Exception as e:
        logger.error(f"Erreur lors de la recherche de la page '{page_name}' dans la BDD '{database_id}': {e}")
        return None

def integrate_with_notion(df_menus_complet):
    """
    Intègre les menus générés dans la base de données Notion 'Menus'.
    """
    if df_menus_complet.empty:
        st.warning("Aucun menu à intégrer. Veuillez générer les menus d'abord.")
        return

    st.info("Début de l'intégration des menus dans Notion...")
    progress_bar = st.progress(0)
    total_menus = len(df_menus_complet)
    integrated_count = 0

    df_recettes = get_recettes_data() # Récupérer les recettes pour les IDs
    recettes_name_to_id = {row[COLONNE_NOM].lower(): row[COLONNE_ID_RECETTE] for _, row in df_recettes.iterrows() if pd.notna(row[COLONNE_NOM])}

    for index, row in df_menus_complet.iterrows():
        recette_nom = row["Recette"]
        date_menu = row[COLONNE_DATE]
        repas_type = row[COLONNE_REPAS_TYPE]
        participants = row[COLONNE_NB_PERSONNES]
        recette_id = row["Recette_ID"] # L'ID de recette généré par MenuGenerator

        if pd.isna(recette_nom) or recette_nom == "À définir" or recette_nom.startswith("Erreur"):
            logger.info(f"Menu ignoré (non défini ou erreur): {date_menu} - {repas_type}")
            integrated_count += 1 # Compter comme traité mais ignoré
            progress_bar.progress((integrated_count / total_menus))
            continue

        # Tenter de trouver l'ID de la recette dans Notion si Recette_ID n'est pas direct ou s'il est None
        notion_recette_id = recette_id # Utiliser l'ID déjà trouvé par le générateur
        if not notion_recette_id:
             # Fallback: chercher par nom si l'ID n'a pas été trouvé par le générateur
            logger.warning(f"ID de recette manquant pour '{recette_nom}', tentative de recherche par nom.")
            notion_recette_id = recettes_name_to_id.get(str(recette_nom).lower())

        if not notion_recette_id:
            st.warning(f"Recette '{recette_nom}' non trouvée dans Notion. Impossible de lier ce menu.")
            integrated_count += 1
            progress_bar.progress((integrated_count / total_menus))
            continue

        # Construire le nom de la page du menu pour la vérification d'existence
        menu_page_name = f"{date_menu} - {repas_type} - {recette_nom}"

        # Vérifier si le menu existe déjà dans Notion pour éviter les doublons
        existing_page_id = get_page_id_by_name(DATABASE_ID_MENUS, COLONNE_NOM, menu_page_name)

        if existing_page_id:
            logger.info(f"Menu '{menu_page_name}' existe déjà. Skipping creation.")
        else:
            try:
                properties = {
                    COLONNE_NOM: {"title": [{"text": {"content": menu_page_name}}]},
                    COLONNE_DATE: {"date": {"start": date_menu}},
                    "Recette": {"relation": [{"id": notion_recette_id}]}, # Lier à la recette par son ID
                    "Repas": {"select": {"name": repas_type}},
                    COLONNE_NB_PERSONNES: {"rich_text": [{"text": {"content": participants}}]}
                }

                notion.pages.create(
                    parent={"database_id": DATABASE_ID_MENUS},
                    properties=properties
                )
                logger.info(f"Menu '{menu_page_name}' créé avec succès dans Notion.")
            except Exception as e:
                st.error(f"Erreur lors de la création du menu '{menu_page_name}' dans Notion : {e}")
                logger.error(f"Détail de l'erreur création page Notion: {e}")

        integrated_count += 1
        progress_bar.progress(integrated_count / total_menus)

    st.success(f"Intégration terminée. {integrated_count} menus traités.")
    progress_bar.empty() # Masquer la barre de progression


# --- Interface utilisateur Streamlit ---

def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus avec Notion")

    st.title("🍽️ Générateur de Menus et Listes de Courses")
    st.markdown("Cette application vous aide à générer des menus hebdomadaires et des listes de courses basés sur votre planning et vos bases de données Notion.")

    # --- Sidebar pour le chargement des données ---
    with st.sidebar:
        st.header("Chargement des Données")
        st.markdown("Cliquez pour recharger toutes les données depuis Notion. Recommandé si vos bases Notion ont été mises à jour.")
        if st.button("Recharger toutes les données Notion"):
            st.cache_data.clear()
            st.session_state.clear()
            st.success("Cache et session effacés. Rechargement des données.")
            # Trigger a rerun to reload data with fresh caches
            st.rerun()

        st.subheader("1. Fichier Planning des Repas (.csv)")
        uploaded_file = st.file_uploader("Téléchargez votre fichier Planning.csv", type="csv",
                                         help="Doit contenir les colonnes 'Date', 'Type_repas', 'Participant(s)' et optionnellement 'Recette_specifique'.")
        if uploaded_file is not None:
            try:
                df_planning = pd.read_csv(uploaded_file)
                st.session_state['df_planning'] = df_planning
                st.success("Fichier Planning.csv chargé avec succès!")
            except Exception as e:
                st.error(f"Erreur lors du chargement du fichier Planning.csv: {e}")
        elif 'df_planning' not in st.session_state:
            st.info("Veuillez télécharger un fichier Planning.csv pour commencer.")

        st.subheader("2. Statut des Données Notion")
        # Charger les données Notion une seule fois et les stocker dans session_state
        if 'df_ingredients' not in st.session_state:
            with st.spinner("Chargement des données ingrédients Notion..."):
                st.session_state['df_ingredients'] = get_ingredients_data()
            st.success(f"Ingrédients chargés : {len(st.session_state['df_ingredients'])} lignes.")

        if 'df_ingredients_recettes' not in st.session_state:
            with st.spinner("Chargement des données ingrédients-recettes Notion..."):
                st.session_state['df_ingredients_recettes'] = get_ingredients_recettes_data()
            st.success(f"Ingrédients-recettes chargés : {len(st.session_state['df_ingredients_recettes'])} lignes.")

        if 'df_recettes' not in st.session_state:
            with st.spinner("Chargement des données recettes Notion..."):
                st.session_state['df_recettes'] = get_recettes_data()
            st.success(f"Recettes chargées : {len(st.session_state['df_recettes'])} lignes.")

        if 'df_existing_menus' not in st.session_state:
            with st.spinner("Chargement des menus existants Notion..."):
                st.session_state['df_existing_menus'] = get_existing_menus_data()
            st.success(f"Menus existants chargés : {len(st.session_state['df_existing_menus'])} lignes.")


    # --- Contenu principal ---

    st.header("1. Vérification des Données Chargées")
    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Planning.csv")
        if 'df_planning' in st.session_state and not st.session_state['df_planning'].empty:
            st.dataframe(st.session_state['df_planning'].head())
            st.write(f"Lignes: {len(st.session_state['df_planning'])}")
        else:
            st.info("Aucun planning chargé.")
    with col2:
        st.subheader("Données Notion (Aperçu)")
        if 'df_ingredients' in st.session_state and not st.session_state['df_ingredients'].empty:
            st.write("Ingrédients:")
            st.dataframe(st.session_state['df_ingredients'].head())
            st.write(f"Lignes: {len(st.session_state['df_ingredients'])}")
        if 'df_ingredients_recettes' in st.session_state and not st.session_state['df_ingredients_recettes'].empty:
            st.write("Ingrédients-Recettes:")
            st.dataframe(st.session_state['df_ingredients_recettes'].head())
            st.write(f"Lignes: {len(st.session_state['df_ingredients_recettes'])}")
        if 'df_recettes' in st.session_state and not st.session_state['df_recettes'].empty:
            st.write("Recettes:")
            st.dataframe(st.session_state['df_recettes'].head())
            st.write(f"Lignes: {len(st.session_state['df_recettes'])}")
        if 'df_existing_menus' in st.session_state and not st.session_state['df_existing_menus'].empty:
            st.write("Menus Existants:")
            st.dataframe(st.session_state['df_existing_menus'].head())
            st.write(f"Lignes: {len(st.session_state['df_existing_menus'])}")


    st.header("2. Générer les menus et listes")
    if st.button("Lancer la Génération des Menus et Listes"):
        if 'df_planning' in st.session_state and \
           'df_ingredients' in st.session_state and \
           'df_ingredients_recettes' in st.session_state and \
           'df_recettes' in st.session_state and \
           'df_existing_menus' in st.session_state:

            with st.spinner("Génération des menus et listes de courses en cours..."):
                df_menus_gen, liste_courses_txt, _ = process_and_generate(
                    st.session_state['df_planning'],
                    st.session_state['df_recettes'],
                    st.session_state['df_ingredients'],
                    st.session_state['df_ingredients_recettes'],
                    st.session_state['df_existing_menus']
                )
                st.session_state['df_menus_generes'] = df_menus_gen
                st.session_state['liste_courses_texte'] = liste_courses_txt

                if not df_menus_gen.empty:
                    st.subheader("Menus Générés")
                    st.dataframe(df_menus_gen)

                    # Bouton de téléchargement CSV
                    csv_buffer = io.StringIO()
                    df_menus_gen.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
                    csv_bytes = csv_buffer.getvalue().encode("utf-8-sig")

                    st.download_button(
                        label="Télécharger Menus_generes.csv",
                        data=csv_bytes,
                        file_name=FICHIER_EXPORT_MENU_CSV,
                        mime="text/csv",
                        key="download_menus_csv"
                    )

                    # Bouton de téléchargement TXT (Liste de courses)
                    st.subheader("Liste de Courses Générée")
                    st.text_area("Votre liste de courses", liste_courses_txt, height=300)
                    st.download_button(
                        label="Télécharger Liste_ingredients.txt",
                        data=liste_courses_txt.encode("utf-8-sig"),
                        file_name=FICHIER_EXPORT_LISTE_TXT,
                        mime="text/plain",
                        key="download_liste_txt"
                    )
                    st.success("Menus et listes prêts au téléchargement.")
                else:
                    st.warning("Aucun menu généré. Vérifiez vos données d'entrée et les filtres.")
        else:
            st.warning("Veuillez charger tous les fichiers nécessaires et les données Notion pour activer la génération.")


    st.header("3. Intégrer avec Notion")
    st.markdown("Activez cette option pour envoyer les menus générés vers votre base de données Notion.")
    
    if 'df_menus_generes' in st.session_state and not st.session_state['df_menus_generes'].empty:
        if st.checkbox("Envoyer les menus générés à Notion?"):
            if st.button("Lancer l'intégration Notion"):
                integrate_with_notion(st.session_state['df_menus_generes'])
    else:
        st.info("Générez d'abord les menus (étape 2) pour activer l'intégration Notion.")


    st.header("4. Extraire les Menus existants depuis Notion")
    st.markdown("Cette section vous permet de télécharger un fichier CSV contenant les menus actuellement enregistrés dans votre base de données Notion.")

    if st.button("Extraire et Télécharger les Menus de Notion"):
        with st.spinner("Extraction en cours depuis Notion..."):
            csv_data_extracted = get_existing_menus_data() # Appelle la fonction qui retourne déjà les données
            if csv_data_extracted is not None and not csv_data_extracted.empty:
                # Convertir le DataFrame en CSV pour le téléchargement
                csv_buffer = io.StringIO()
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


    st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")


if __name__ == "__main__":
    main()
