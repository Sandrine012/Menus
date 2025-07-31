import streamlit as st
import pandas as pd
import logging
import re
from datetime import datetime
from notion_client import Client
from notion_client.helpers import get_id
from notion_client.errors import RequestTimeoutError # Importer l'erreur spécifique
import time
import httpx # Garder pour le type hint ou si on attrape les deux
import io
import csv # Nécessaire pour l'écriture CSV dans la fonction d'extraction
import unicodedata # Nécessaire pour la fonction d'extraction

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes ---
FICHIER_SORTIE_MENU_CSV = "Menus_generes.csv"
FICHIER_SORTIE_LISTES_TXT = "Listes_ingredients.txt"
FICHIER_EXPORT_NOTION_CSV = "Menus_extraits_Notion.csv" # Nouveau nom pour le fichier exporté

# --- Connexion à Notion ---
# Récupérer la clé API et l'ID de la base de données depuis les secrets Streamlit
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    # Utilisation des IDs de base de données spécifiques
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Le secret Notion manquant est : {e}. "
             "Veuillez configurer tous les secrets Notion dans le fichier .streamlit/secrets.toml "
             "ou via l'interface Streamlit Cloud. "
             "Assurez-vous d'avoir: notion_api_key, notion_database_id_menus, "
             "notion_database_id_recettes, notion_database_id_ingredients, "
             "notion_database_id_ingredients_recettes.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas trouvés.
except Exception as e:
    st.error(f"Erreur lors de l'initialisation du client Notion : {e}")
    st.stop()

# --- Fonctions Notion existantes ---
def query_database(database_id, filter_property=None, filter_value=None):
    try:
        if filter_property and filter_value:
            filter_obj = {
                "property": filter_property,
                "text": {
                    "contains": filter_value
                }
            }
            results = notion.databases.query(database_id=database_id, filter=filter_obj).get("results")
        else:
            results = notion.databases.query(database_id=database_id).get("results")
        return results
    except Exception as e:
        st.error(f"Erreur lors de la requête Notion sur la base {database_id}: {e}")
        logger.error(f"Erreur lors de la requête Notion sur la base {database_id}: {e}")
        return []

def get_page_properties(page):
    properties = {}
    for prop_name, prop_data in page["properties"].items():
        if prop_data["type"] == "title":
            properties[prop_name] = prop_data["title"][0]["plain_text"] if prop_data["title"] else ""
        elif prop_data["type"] == "rich_text":
            properties[prop_name] = prop_data["rich_text"][0]["plain_text"] if prop_data["rich_text"] else ""
        elif prop_data["type"] == "multi_select":
            properties[prop_name] = [item["name"] for item in prop_data["multi_select"]]
        elif prop_data["type"] == "select":
            properties[prop_name] = prop_data["select"]["name"] if prop_data["select"] else ""
        elif prop_data["type"] == "number":
            properties[prop_name] = prop_data["number"]
        elif prop_data["type"] == "checkbox":
            properties[prop_name] = prop_data["checkbox"]
        elif prop_data["type"] == "date":
            properties[prop_name] = prop_data["date"]["start"] if prop_data["date"] else ""
        elif prop_data["type"] == "url":
            properties[prop_name] = prop_data["url"]
        elif prop_data["type"] == "relation":
            properties[prop_name] = [item["id"] for item in prop_data["relation"]]
        elif prop_data["type"] == "formula":
            if prop_data["formula"]["type"] == "string":
                properties[prop_name] = prop_data["formula"]["string"]
            elif prop_data["formula"]["type"] == "number":
                properties[prop_name] = prop_data["formula"]["number"]
            elif prop_data["formula"]["type"] == "boolean":
                properties[prop_name] = prop_data["formula"]["boolean"]
            elif prop_data["formula"]["type"] == "date":
                properties[prop_name] = prop_data["formula"]["date"]["start"] if prop_data["formula"]["date"] else ""
        else:
            properties[prop_name] = None
    return properties

def create_page(database_id, properties):
    try:
        new_page = notion.pages.create(parent={"database_id": database_id}, properties=properties)
        return new_page
    except Exception as e:
        st.error(f"Erreur lors de la création d'une page dans Notion: {e}")
        logger.error(f"Erreur lors de la création d'une page dans Notion: {e}")
        return None

def update_page_property(page_id, property_name, property_type, value):
    try:
        properties = {}
        if property_type == "rich_text":
            properties[property_name] = {"rich_text": [{"text": {"content": value}}]}
        elif property_type == "date":
            properties[property_name] = {"date": {"start": value}}
        elif property_type == "select":
            properties[property_name] = {"select": {"name": value}}
        elif property_type == "relation":
            properties[property_name] = {"relation": [{"id": item_id} for item_id in value]}
        elif property_type == "number":
            properties[property_name] = {"number": value}
        elif property_type == "checkbox":
            properties[property_name] = {"checkbox": value}
        else:
            st.warning(f"Type de propriété non géré pour la mise à jour : {property_type}")
            return False

        notion.pages.update(page_id=page_id, properties=properties)
        return True
    except Exception as e:
        st.error(f"Erreur lors de la mise à jour de la propriété '{property_name}' de la page {page_id}: {e}")
        logger.error(f"Erreur lors de la mise à jour de la propriété '{property_name}' de la page {page_id}: {e}")
        return False

def get_page_id_by_name(database_id, page_name_property, page_name):
    try:
        results = notion.databases.query(
            database_id=database_id,
            filter={
                "property": page_name_property,
                "title": {
                    "equals": page_name
                }
            }
        ).get("results")
        if results:
            return results[0]["id"]
        return None
    except Exception as e:
        st.error(f"Erreur lors de la recherche de l'ID de la page '{page_name}' dans la base {database_id}: {e}")
        logger.error(f"Erreur lors de la recherche de l'ID de la page '{page_name}' dans la base {database_id}: {e}")
        return None

# --- Fonctions de traitement des données existantes ---
def process_data(df_planning, df_recettes, df_ingredients, df_ingredients_recettes):
    st.info("Traitement des données en cours...")
    logger.info("Début du traitement des données.")

    # Nettoyage des noms de colonnes : suppression des espaces superflus et caractères spéciaux
    df_planning.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_planning.columns]
    df_recettes.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_recettes.columns]
    df_ingredients.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_ingredients.columns]
    df_ingredients_recettes.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_ingredients_recettes.columns]

    df_planning['date'] = pd.to_datetime(df_planning['date'], format='%d/%m/%Y')
    df_planning.set_index('date', inplace=True)

    df_recettes['nom'] = df_recettes['nom'].str.strip()

    df_ingredients_recettes['recette'] = df_ingredients_recettes['recette'].str.strip()
    df_ingredients_recettes['ingredient'] = df_ingredients_recettes['ingredient'].str.strip()

    df_ingredients['nom'] = df_ingredients['nom'].str.strip()

    # Fusion des dataframes
    df_menus = df_planning.stack().reset_index()
    df_menus.columns = ['date', 'repas_type', 'recette_nom']
    df_menus['date'] = df_menus['date'].dt.strftime('%d/%m/%Y')

    # Remplacer les valeurs vides ou "None" par une chaîne vide
    df_menus['recette_nom'] = df_menus['recette_nom'].fillna('').astype(str).str.strip()

    # Nettoyer les noms des colonnes 'repas_type' pour correspondre aux propriétés Notion
    df_menus['repas_type'] = df_menus['repas_type'].str.replace('_', ' ').str.title()
    df_menus['repas_type'] = df_menus['repas_type'].replace({
        'Dejeuner': 'Déjeuner',
        'Diner': 'Dîner'
    })

    df_menus_complet = pd.merge(df_menus, df_recettes, left_on='recette_nom', right_on='nom', how='left')
    df_menus_complet.rename(columns={'nom': 'Nom Recette', 'participants': 'Participant(s)'}, inplace=True)

    # Utilisation de DATABASE_ID_RECETTES pour chercher l'ID de la recette
    df_menus_complet['Recette ID'] = df_menus_complet['recette_nom'].apply(lambda x: get_page_id_by_name(DATABASE_ID_RECETTES, "Nom", x) if x else None) # Assurez-vous que "Nom" est la propriété de titre de votre base de recettes

    st.success("Traitement des données terminé.")
    logger.info("Fin du traitement des données.")

    return df_menus_complet, df_ingredients, df_ingredients_recettes

def generate_output_files(df_menus_complet, df_ingredients, df_ingredients_recettes):
    st.info("Génération des fichiers de sortie en cours...")
    logger.info("Début de la génération des fichiers de sortie.")

    # Préparation du DataFrame pour l'export CSV
    df_menu_genere = df_menus_complet[['date', 'Participant(s)', 'recette_nom']].copy()
    df_menu_genere.rename(columns={'date': 'Date', 'recette_nom': 'Nom'}, inplace=True)

    # Formater les dates pour Notion au format YYYY-MM-DD HH:MM
    # La date dans df_menu_genere est déjà au format DD/MM/YYYY
    # Pour l'export Notion, on peut ajouter une heure par défaut si nécessaire
    df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'], format="%d/%m/%Y", errors='coerce').dt.strftime('%Y-%m-%d %H:%M')

    # Exporter en CSV pour téléchargement
    csv_buffer = io.StringIO()
    df_menu_genere.to_csv(csv_buffer, index=False, encoding="utf-8-sig")
    csv_data = csv_buffer.getvalue().encode("utf-8-sig")
    st.download_button(
        label="Télécharger Menus_generes.csv",
        data=csv_data,
        file_name=FICHIER_SORTIE_MENU_CSV,
        mime="text/csv",
    )
    logger.info(f"Fichier CSV '{FICHIER_SORTIE_MENU_CSV}' prêt pour téléchargement.")

    # Génération du récapitulatif des ingrédients
    df_details_ingredients = pd.merge(df_menus_complet, df_ingredients_recettes, left_on='recette_nom', right_on='recette', how='inner')
    df_details_ingredients = pd.merge(df_details_ingredients, df_ingredients, left_on='ingredient', right_on='nom', how='inner', suffixes=('_recette', '_stock'))

    df_details_ingredients['quantite'] = pd.to_numeric(df_details_ingredients['quantite'], errors='coerce').fillna(0)

    # Calcul des quantités totales par ingrédient et unité
    liste_courses = df_details_ingredients.groupby(['ingredient', 'unite'])['quantite'].sum().reset_index()

    # Comparaison avec le stock (si la colonne 'quantite_stock' existe et est numérique)
    if 'quantite_stock' in df_ingredients.columns:
        df_ingredients['quantite_stock'] = pd.to_numeric(df_ingredients['quantite_stock'], errors='coerce').fillna(0)
        liste_courses = pd.merge(liste_courses, df_ingredients[['nom', 'quantite_stock']], left_on='ingredient', right_on='nom', how='left').drop(columns='nom')
        liste_courses['A acheter'] = liste_courses['quantite'] - liste_courses['quantite_stock']
        liste_courses['A acheter'] = liste_courses['A acheter'].apply(lambda x: max(0, x)) # Ne pas afficher de quantités négatives

        # Filtre pour n'afficher que ce qui est à acheter
        liste_courses = liste_courses[liste_courses['A acheter'] > 0]
        st.subheader("Liste de courses (éléments à acheter) :")
        contenu_fichier_recap_txt = ["Liste de courses (éléments à acheter) :\n"]
        for _, row in liste_courses.iterrows():
            line = f"- {row['A acheter']:.2f} {row['unite']} de {row['ingredient']}\n"
            contenu_fichier_recap_txt.append(line)
            st.write(line.strip()) # Afficher aussi dans l'app
    else:
        st.subheader("Récapitulatif des ingrédients requis (sans comparaison de stock) :")
        contenu_fichier_recap_txt = ["Récapitulatif des ingrédients requis :\n"]
        for _, row in liste_courses.iterrows():
            line = f"- {row['quantite']:.2f} {row['unite']} de {row['ingredient']}\n"
            contenu_fichier_recap_txt.append(line)
            st.write(line.strip()) # Afficher aussi dans l'app

    txt_buffer = io.StringIO()
    txt_buffer.writelines(contenu_fichier_recap_txt)
    txt_data = txt_buffer.getvalue().encode("utf-8")
    st.download_button(
        label="Télécharger Liste_ingredients.txt",
        data=txt_data,
        file_name=FICHIER_SORTIE_LISTES_TXT,
        mime="text/plain",
    )
    logger.info(f"Fichier TXT '{FICHIER_SORTIE_LISTES_TXT}' prêt pour téléchargement.")
    st.success("Génération des fichiers de sortie terminée.")


# --- Fonction d'intégration Notion (à adapter si nécessaire) ---
def integrate_with_notion(df_menus_complet): # Suppression de database_id du paramètre pour utiliser DATABASE_ID_MENUS directement
    st.info("Intégration avec Notion en cours...")
    logger.info("Début de l'intégration avec Notion.")

    # Filtrer les lignes qui n'ont pas de recette_nom vide
    df_to_integrate = df_menus_complet[df_menus_complet['recette_nom'] != ''].copy()

    if df_to_integrate.empty:
        st.warning("Aucun menu valide à intégrer dans Notion.")
        logger.warning("Aucun menu valide à intégrer dans Notion.")
        return

    # Vérifier l'existence des pages de recettes dans Notion pour récupérer les IDs de relation
    recette_ids = {}
    st.info("Vérification des recettes existantes dans Notion...")
    for recette_nom in df_to_integrate['recette_nom'].unique():
        # Utilisation de DATABASE_ID_RECETTES pour chercher la recette
        page_id = get_page_id_by_name(DATABASE_ID_RECETTES, "Nom", recette_nom) # Assurez-vous que "Nom" est la propriété de titre de votre base de recettes
        if page_id:
            recette_ids[recette_nom] = page_id
        else:
            st.warning(f"La recette '{recette_nom}' n'a pas été trouvée dans Notion. Elle ne sera pas liée.")


    for index, row in df_to_integrate.iterrows():
        date_str = row['date'] # Format DD/MM/YYYY
        repas_type = row['repas_type'] # Déjeuner, Dîner, Petit-déjeuner, Goûter
        recette_nom = row['recette_nom']
        participants = row['Participant(s)']

        # Propriétés de la nouvelle page de menu
        properties = {
            "Date": {
                "date": {
                    "start": datetime.strptime(date_str, '%d/%m/%Y').isoformat() # Convertir en format ISO 8601
                }
            },
            "Repas": {
                "select": {
                    "name": repas_type
                }
            },
            "Nom": { # C'est le titre de la page de menu
                "title": [
                    {
                        "text": {
                            "content": f"{repas_type} - {recette_nom} ({date_str})"
                        }
                    }
                ]
            },
            "Participant(s)": {
                "rich_text": [
                    {
                        "text": {
                            "content": str(participants)
                        }
                    }
                ]
            }
        }

        # Ajouter la relation à la recette si l'ID est trouvé
        if recette_nom in recette_ids:
            properties["Recette"] = { # Assurez-vous que "Recette" est le nom de votre propriété de relation dans la base "Planning Menus"
                "relation": [{"id": recette_ids[recette_nom]}]
            }
        else:
            st.warning(f"Impossible de lier la recette '{recette_nom}' pour le {repas_type} du {date_str} car elle n'a pas été trouvée dans Notion.")


        # Vérifier si la page existe déjà pour éviter les doublons
        # On suppose que le titre "Nom" est unique pour un même jour et repas
        # Utilisation de DATABASE_ID_MENUS pour chercher la page de menu existante
        existing_page_id = get_page_id_by_name(DATABASE_ID_MENUS, "Nom", properties["Nom"]["title"][0]["text"]["content"])

        if existing_page_id:
            st.info(f"La page pour '{repas_type} - {recette_nom} ({date_str})' existe déjà. Mise à jour en cours...")
            # Ici, vous pouvez choisir de mettre à jour ou de sauter
            # Pour l'exemple, nous ne ferons rien de plus qu'informer
            # update_page_property(existing_page_id, "Participant(s)", "rich_text", str(participants))
            pass
        else:
            st.info(f"Création de la page pour '{repas_type} - {recette_nom} ({date_str})'...")
            # Utilisation de DATABASE_ID_MENUS pour créer la page de menu
            create_page(DATABASE_ID_MENUS, properties)
    st.success("Intégration avec Notion terminée.")
    logger.info("Fin de l'intégration avec Notion.")

# --- Nouvelle Fonction d'extraction de Notion (intégrée) ---
def extract_notion_menus_to_csv(notion_client, database_id_to_extract, csv_export_filename):
    st.info(f"Début de l'extraction des menus depuis Notion vers '{csv_export_filename}'...")
    logger.info(f"Début de l'extraction des menus depuis Notion vers '{csv_export_filename}'...")

    # --- NOMS DES PROPRIÉTÉS NOTION (À MODIFIER ICI SI NÉCESSAIRE) ---
    # Ces noms sont spécifiques à la base de données source pour l'extraction
    nom_menu_property_name = "Nom Menu"
    recette_property_name = "Recette"
    date_property_name = "Date"
    # --- FIN DES NOMS DE PROPRIÉTÉS NOTION ---

    # Paramètres d'extraction
    num_rows_to_extract = 100000
    next_cursor = None
    batch_size = 25
    max_retries = 7
    retry_delay = 10
    retries = 0
    api_timeout_seconds = 180
    
    total_extracted = 0
    
    csv_buffer = io.StringIO()
    csv_writer = csv.writer(csv_buffer, quoting=csv.QUOTE_MINIMAL, dialect='excel')
    header = [nom_menu_property_name, recette_property_name, date_property_name]
    csv_writer.writerow(header)

    try:
        # Test de connexion initiale à la base de données
        try:
            database_info = notion_client.databases.retrieve(database_id_to_extract)
            logger.info(f"Informations de la base de données récupérées avec succès: {database_info.get('title')}")
            st.success(f"Connecté à la base de données Notion: {database_info.get('title')}")
        except Exception as e:
            st.error(f"Erreur lors de la récupération des informations de la base de données initiale. Vérifiez votre jeton et l'ID de la base de données : {e}")
            logger.error(f"Erreur lors de la récupération des informations de la base de données initiale. Vérifiez votre jeton et l'ID de la base de données : {e}")
            return None # Retourne None si la connexion échoue

        while total_extracted < num_rows_to_extract:
            page_start_time = time.time()
            try:
                request_batch_size = batch_size
                logger.debug(f"Demande à Notion: start_cursor={next_cursor}, page_size={request_batch_size}")

                results = notion_client.databases.query(
                    database_id=database_id_to_extract,
                    start_cursor=next_cursor,
                    page_size=request_batch_size,
                    filter={
                        "and": [
                            {"property": recette_property_name, "relation": {"is_not_empty": True}},
                        ]
                    },
                    timeout=api_timeout_seconds
                )

                retries = 0
                retry_delay = 10

                page_results = results.get("results", [])
                if not page_results:
                    logger.info("Aucun résultat retourné par l'API ou fin de la base de données atteinte.")
                    break

                st.info(f"Reçu {len(page_results)} menus de Notion. Traitement ({nom_menu_property_name}, {recette_property_name}, {date_property_name})...")
                page_valid_count = 0

                for result in page_results:
                    if total_extracted >= num_rows_to_extract:
                        break

                    row_values = []
                    properties = result.get("properties", {})

                    # 1. Nom Menu
                    nom_menu_value = ""
                    if nom_menu_property_name in properties:
                        name_property = properties[nom_menu_property_name]
                        nom_menu_value = "".join([text.get("plain_text", "") for text in name_property.get("title", []) or name_property.get("rich_text", [])])
                    row_values.append(nom_menu_value.strip())

                    # 2. Recette (Gestion de la relation - Extraction des IDs)
                    recette_value = ""
                    if recette_property_name in properties:
                        prop = properties[recette_property_name]
                        if prop["type"] == "relation" and prop["relation"]:
                            recette_value = ", ".join([relation["id"] for relation in prop["relation"]])
                        elif prop["type"] == "rollup" and prop["rollup"]:
                            rollup_data = prop["rollup"]
                            if rollup_data.get("type") == "array" and rollup_data.get("array"):
                                recette_ids = []
                                for item in rollup_data["array"]:
                                    if item.get("id"):
                                        recette_ids.append(item["id"])
                                    elif item.get("relation"):
                                        recette_ids.extend([rel.get("id") for rel in item["relation"] if rel.get("id")])
                                recette_value = ", ".join(recette_ids)
                    row_values.append(recette_value)

                    # 3. Date (Formatage de la date)
                    date_value = ""
                    if date_property_name in properties:
                        date_property = properties[date_property_name]
                        if date_property["type"] == "date" and date_property.get("date") and date_property["date"].get("start"):
                            date_str = date_property["date"]["start"]
                            date_object = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                            date_value = date_object.strftime('%Y-%m-%d')
                    row_values.append(date_value)

                    csv_writer.writerow(row_values)
                    total_extracted += 1
                    page_valid_count += 1

                page_end_time = time.time()
                st.info(f"Page traitée en {page_end_time - page_start_time:.2f} sec. {page_valid_count} menus ajoutés (Total: {total_extracted}/{num_rows_to_extract}).")
                logger.info(f"Page traitée en {page_end_time - page_start_time:.2f} sec. {page_valid_count} menus ajoutés (Total: {total_extracted}/{num_rows_to_extract}).")

                if total_extracted >= num_rows_to_extract:
                    st.info(f"Limite de {num_rows_to_extract} menus atteinte.")
                    logger.info(f"Limite de {num_rows_to_extract} menus atteinte.")
                    break
                next_cursor = results.get("next_cursor")
                if not next_cursor:
                    st.info("Fin des pages retournées par l'API (next_cursor est None).")
                    logger.info("Fin des pages retournées par l'API (next_cursor est None).")
                    break

                st.info(f"Passage à la page suivante (next_cursor: {str(next_cursor)[:10]}...).")
                logger.info(f"Passage à la page suivante (next_cursor: {str(next_cursor)[:10]}...).")
                time.sleep(1)

            except (httpx.TimeoutException, RequestTimeoutError) as e:
                retries += 1
                st.warning(f"Timeout détecté ({type(e).__name__}). Tentative {retries}/{max_retries}...")
                logger.warning(f"Timeout détecté ({type(e).__name__}). Tentative {retries}/{max_retries}...")
                if retries >= max_retries:
                    st.error(f"Nombre maximum de tentatives atteint ({max_retries}) après Timeout. Abandon.")
                    logger.error(f"Nombre maximum de tentatives atteint ({max_retries}) après Timeout. Abandon.")
                    break

                st.warning(f"Attente de {retry_delay} secondes avant la nouvelle tentative...")
                logger.warning(f"Attente de {retry_delay} secondes avant la nouvelle tentative...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue

            except Exception as e:
                st.exception(f"Une erreur inattendue et non liée au timeout est survenue pendant la boucle : {e}")
                logger.exception(f"Une erreur inattendue et non liée au timeout est survenue pendant la boucle : {e}")
                break

        final_message = f"Extraction terminée. {total_extracted} menus extraits et prêts à télécharger."
        st.success(final_message)
        logger.info(final_message)

        return csv_buffer.getvalue().encode("utf-8-sig")

    except Exception as e:
        st.error(f"Erreur générale lors de l'extraction Notion : {e}")
        logger.exception(f"Erreur générale lors de l'extraction Notion : {e}")
        return None

# --- Application Streamlit principale ---
st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
st.title("🍽️ Générateur de Menus pour Notion")

st.markdown("""
Cette application vous permet de générer des menus, des listes d'ingrédients,
et de les synchroniser avec votre base de données Notion "Planning Menus".
""")

st.header("1. Téléchargez vos fichiers CSV")
st.warning("Assurez-vous que vos fichiers CSV contiennent les colonnes attendues (voir exemple).")

uploaded_planning = st.file_uploader("Chargez le fichier Planning.csv", type="csv")
uploaded_recettes = st.file_uploader("Chargez le fichier Recettes.csv", type="csv")
uploaded_ingredients = st.file_uploader("Chargez le fichier Ingredients.csv", type="csv")
uploaded_ingredients_recettes = st.file_uploader("Chargez le fichier Ingredients_recettes.csv", type="csv")

df_planning, df_recettes, df_ingredients, df_ingredients_recettes = [None] * 4

if uploaded_planning is not None:
    try:
        df_planning = pd.read_csv(uploaded_planning)
        st.success("Planning.csv chargé avec succès.")
    except Exception as e:
        st.error(f"Erreur de lecture de Planning.csv : {e}")
else:
    st.info("Veuillez charger le fichier Planning.csv pour commencer.")

if uploaded_recettes is not None:
    try:
        df_recettes = pd.read_csv(uploaded_recettes)
        st.success("Recettes.csv chargé avec succès.")
    except Exception as e:
        st.error(f"Erreur de lecture de Recettes.csv : {e}")
else:
    st.info("Veuillez charger le fichier Recettes.csv.")

if uploaded_ingredients is not None:
    try:
        df_ingredients = pd.read_csv(uploaded_ingredients)
        st.success("Ingredients.csv chargé avec succès.")
    except Exception as e:
        st.error(f"Erreur de lecture de Ingredients.csv : {e}")
else:
    st.info("Veuillez charger le fichier Ingredients.csv.")

if uploaded_ingredients_recettes is not None:
    try:
        df_ingredients_recettes = pd.read_csv(uploaded_ingredients_recettes)
        st.success("Ingredients_recettes.csv chargé avec succès.")
    except Exception as e:
        st.error(f"Erreur de lecture de Ingredients_recettes.csv : {e}")
else:
    st.info("Veuillez charger le fichier Ingredients_recettes.csv.")


if all(df is not None for df in [df_planning, df_recettes, df_ingredients, df_ingredients_recettes]):
    st.header("2. Générer les menus et listes")
    if st.button("Générer les Menus et Listes"):
        with st.spinner("Génération en cours..."):
            df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed = process_data(
                df_planning, df_recettes, df_ingredients, df_ingredients_recettes
            )

            if not df_menus_complet.empty:
                st.subheader("Aperçu des Menus Générés :")
                st.dataframe(df_menus_complet[['date', 'repas_type', 'recette_nom', 'Participant(s)']])

                generate_output_files(df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed)

                st.header("3. Intégrer avec Notion")
                notion_integrate = st.checkbox("Envoyer les menus générés à Notion?")
                if notion_integrate:
                    if st.button("Lancer l'intégration Notion"):
                        with st.spinner("Intégration Notion en cours..."):
                            integrate_with_notion(df_menus_complet) # Plus besoin de passer database_id
                            st.success("Processus d'intégration Notion terminé.")
            else:
                st.warning("Aucun menu n'a pu être généré. Veuillez vérifier vos fichiers.")
else:
    st.warning("Veuillez charger tous les fichiers CSV nécessaires pour activer la génération.")


st.header("4. Extraire les Menus existants depuis Notion")
st.markdown("Cette section vous permet de télécharger un fichier CSV contenant les menus actuellement enregistrés dans votre base de données Notion.")

if st.button("Extraire et Télécharger les Menus de Notion"):
    with st.spinner("Extraction en cours depuis Notion..."):
        # Utilisation de DATABASE_ID_MENUS pour l'extraction
        csv_data_extracted = extract_notion_menus_to_csv(notion, DATABASE_ID_MENUS, FICHIER_EXPORT_NOTION_CSV)
        if csv_data_extracted:
            st.download_button(
                label="Télécharger Menus_extraits_Notion.csv",
                data=csv_data_extracted,
                file_name=FICHIER_EXPORT_NOTION_CSV,
                mime="text/csv",
            )
            st.success("Fichier d'extraction Notion prêt au téléchargement.")
        else:
            st.error("L'extraction depuis Notion a échoué ou n'a retourné aucune donnée.")


st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")
