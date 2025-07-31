import streamlit as st
import pandas as pd
import logging
import re
from datetime import datetime
from notion_client import Client
from notion_client.helpers import get_id
import io

# --- Configuration du logger ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constantes ---
FICHIER_SORTIE_MENU_CSV = "Menus_generes.csv"
FICHIER_SORTIE_LISTES_TXT = "Listes_ingredients.txt"

# --- Connexion à Notion ---
# Récupérer la clé API et les IDs des bases de données depuis les secrets Streamlit
# Assurez-vous d'avoir un fichier .streamlit/secrets.toml dans votre repo GitHub
# ou configuré les secrets dans Streamlit Cloud.
try:
    NOTION_API_KEY = st.secrets["notion_api_key"]
    # Utilisation des noms de secrets spécifiques pour chaque base de données
    DATABASE_ID_MENUS = st.secrets["notion_database_id_menus"]
    DATABASE_ID_RECETTES = st.secrets["notion_database_id_recettes"]
    DATABASE_ID_INGREDIENTS = st.secrets["notion_database_id_ingredients"]
    DATABASE_ID_INGREDIENTS_RECETTES = st.secrets["notion_database_id_ingredients_recettes"]

    notion = Client(auth=NOTION_API_KEY)
except KeyError as e:
    st.error(f"Erreur : Le secret Notion '{e}' n'est pas configuré. "
             "Veuillez vérifier les noms de vos secrets dans .streamlit/secrets.toml ou l'interface Streamlit Cloud.")
    st.stop() # Arrête l'exécution de l'application si les secrets ne sont pas trouvés.
except Exception as e:
    st.error(f"Erreur lors de l'initialisation du client Notion : {e}")
    st.stop()

# --- Fonctions Notion ---
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
            # Pour les relations, nous récupérons les IDs. Il faudra ensuite les mapper aux noms si nécessaire.
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
                properties[prop_name] = None # Gérer d'autres types de formules si besoin
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
    """
    Recherche l'ID d'une page Notion par le nom de sa propriété de titre.
    page_name_property doit être le nom de la colonne Notion qui est le "Titre".
    """
    try:
        results = notion.databases.query(
            database_id=database_id,
            filter={
                "property": page_name_property,
                "title": { # Le type de la propriété de titre est 'title'
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

# --- Fonctions de traitement des données ---
def process_data(df_planning, df_recettes, df_ingredients, df_ingredients_recettes, id_to_name_map):
    st.info("Traitement des données en cours...")
    logger.info("Début du traitement des données.")

    # Nettoyage des noms de colonnes : suppression des espaces superflus et caractères spéciaux
    # Ceci est important car les noms de colonnes Notion peuvent contenir des espaces/caractères spéciaux.
    # Assurez-vous que les DataFrames Notion ont déjà été renommés pour correspondre à cette logique.
    df_planning.columns = [col.strip().replace(" ", "_").replace("(", "").replace(")", "").replace("é", "e").replace("à", "a").replace("ç", "c").lower() for col in df_planning.columns]
    # Les autres DataFrames (recettes, ingredients, ingredients_recettes) devraient déjà avoir des noms de colonnes cohérents
    # après la conversion depuis Notion et le renommage dans la fonction load_data_from_notion.

    df_planning['date'] = pd.to_datetime(df_planning['date'], format='%d/%m/%Y')
    df_planning.set_index('date', inplace=True)

    # Assurez-vous que df_recettes a une colonne 'nom' et 'participants'
    if 'nom' not in df_recettes.columns or 'participants' not in df_recettes.columns:
        st.error("Le DataFrame des recettes de Notion ne contient pas les colonnes 'nom' ou 'participants' requises.")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame() # Retourne des DFs vides pour éviter des erreurs

    df_recettes['nom'] = df_recettes['nom'].str.strip()

    # Assurez-vous que df_ingredients_recettes a une colonne 'recette_nom', 'ingredient_nom', 'quantite', 'unite'
    if 'recette_nom' not in df_ingredients_recettes.columns or \
       'ingredient_nom' not in df_ingredients_recettes.columns or \
       'quantite' not in df_ingredients_recettes.columns or \
       'unite' not in df_ingredients_recettes.columns:
        st.error("Le DataFrame des ingrédients par recette de Notion ne contient pas les colonnes requises ('recette_nom', 'ingredient_nom', 'quantite', 'unite').")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df_ingredients_recettes['recette_nom'] = df_ingredients_recettes['recette_nom'].str.strip()
    df_ingredients_recettes['ingredient_nom'] = df_ingredients_recettes['ingredient_nom'].str.strip()


    # Assurez-vous que df_ingredients a une colonne 'nom' et 'quantite_stock' (optionnel)
    if 'nom' not in df_ingredients.columns:
        st.error("Le DataFrame des ingrédients de Notion ne contient pas la colonne 'nom' requise.")
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

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

    # Récupérer l'ID de la recette pour la relation Notion, en utilisant le mapping ID->Nom
    # Note: DATABASE_ID_RECETTES est la base des recettes où se trouve le nom.
    df_menus_complet['Recette ID'] = df_menus_complet['recette_nom'].apply(
        lambda x: get_page_id_by_name(DATABASE_ID_RECETTES, "Nom", x) if x else None
    )

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
    # Assurez-vous que 'ingredient_nom' et 'recette_nom' sont les noms de colonnes après le traitement Notion
    df_details_ingredients = pd.merge(df_menus_complet, df_ingredients_recettes, left_on='recette_nom', right_on='recette_nom', how='inner')
    df_details_ingredients = pd.merge(df_details_ingredients, df_ingredients, left_on='ingredient_nom', right_on='nom', how='inner', suffixes=('_recette', '_stock'))

    df_details_ingredients['quantite'] = pd.to_numeric(df_details_ingredients['quantite'], errors='coerce').fillna(0)

    # Calcul des quantités totales par ingrédient et unité
    # Les colonnes 'ingredient' et 'unite' doivent être présentes dans df_details_ingredients
    # Si 'unite' vient de df_ingredients_recettes, assurez-vous de la conserver.
    liste_courses = df_details_ingredients.groupby(['ingredient_nom', 'unite_recette'])['quantite'].sum().reset_index()
    liste_courses.rename(columns={'ingredient_nom': 'ingredient', 'unite_recette': 'unite'}, inplace=True) # Renommer pour la sortie

    # Comparaison avec le stock (si la colonne 'quantite_stock' existe et est numérique dans df_ingredients)
    if 'quantite_stock' in df_ingredients.columns:
        df_ingredients['quantite_stock'] = pd.to_numeric(df_ingredients['quantite_stock'], errors='coerce').fillna(0)
        liste_courses = pd.merge(liste_courses, df_ingredients[['nom', 'quantite_stock']], left_on='ingredient', right_on='nom', how='left').drop(columns='nom')
        liste_courses['A acheter'] = liste_courses['quantite'] - liste_courses['quantite_stock']
        liste_courses['A acheter'] = liste_courses['A acheter'].apply(lambda x: max(0, x)) # Ne pas afficher de quantités négatives

        # Filtre pour n'afficher que ce qui est à acheter
        liste_courses = liste_courses[liste_courses['A acheter'] > 0]
        st.subheader("Liste de courses (éléments à acheter) :")
        contenu_fichier_recap_txt = ["Liste de courses (éléments à acheter) :\n"]
        if not liste_courses.empty:
            for _, row in liste_courses.iterrows():
                line = f"- {row['A acheter']:.2f} {row['unite']} de {row['ingredient']}\n"
                contenu_fichier_recap_txt.append(line)
                st.write(line.strip()) # Afficher aussi dans l'app
        else:
            st.info("Rien à acheter, votre stock est suffisant !")
            contenu_fichier_recap_txt.append("Rien à acheter, votre stock est suffisant !\n")
    else:
        st.subheader("Récapitulatif des ingrédients requis (sans comparaison de stock) :")
        contenu_fichier_recap_txt = ["Récapitulatif des ingrédients requis :\n"]
        if not liste_courses.empty:
            for _, row in liste_courses.iterrows():
                line = f"- {row['quantite']:.2f} {row['unite']} de {row['ingredient']}\n"
                contenu_fichier_recap_txt.append(line)
                st.write(line.strip()) # Afficher aussi dans l'app
        else:
            st.info("Aucun ingrédient requis pour les menus planifiés.")
            contenu_fichier_recap_txt.append("Aucun ingrédient requis pour les menus planifiés.\n")

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


# --- Fonction d'intégration Notion ---
def integrate_with_notion(df_menus_complet, database_id_menus, id_to_name_map):
    st.info("Intégration avec Notion en cours...")
    logger.info("Début de l'intégration avec Notion.")

    # Filtrer les lignes qui n'ont pas de recette_nom vide
    df_to_integrate = df_menus_complet[df_menus_complet['recette_nom'] != ''].copy()

    if df_to_integrate.empty:
        st.warning("Aucun menu valide à intégrer dans Notion.")
        logger.warning("Aucun menu valide à intégrer dans Notion.")
        return

    # Pré-charger les IDs des recettes pour éviter des requêtes répétées dans la boucle
    # Cette information est déjà dans df_menus_complet['Recette ID']
    # Vous pouvez améliorer cela en faisant un mapping de tous les noms de recettes uniques vers leurs IDs une seule fois.
    # Pour l'instant, on se base sur la colonne 'Recette ID' déjà calculée.


    for index, row in df_to_integrate.iterrows():
        date_str = row['date'] # Format DD/MM/YYYY
        repas_type = row['repas_type'] # Déjeuner, Dîner, Petit-déjeuner, Goûter
        recette_nom = row['recette_nom']
        participants = row['Participant(s)']
        recette_notion_id = row['Recette ID'] # L'ID de la page de la recette dans Notion

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
            "Nom": { # C'est le titre de la page de menu dans la base "Planning Menus"
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
        if recette_notion_id:
            # "Recette" doit être le nom de la propriété de relation dans votre base "Planning Menus"
            properties["Recette"] = {
                "relation": [{"id": recette_notion_id}]
            }
        else:
            st.warning(f"Impossible de lier la recette '{recette_nom}' pour le {repas_type} du {date_str} car son ID n'a pas été trouvé dans Notion.")


        # Vérifier si la page existe déjà pour éviter les doublons
        # On suppose que le titre "Nom" est unique pour un même jour et repas
        existing_page_id = get_page_id_by_name(database_id_menus, "Nom", properties["Nom"]["title"][0]["text"]["content"])

        if existing_page_id:
            st.info(f"La page pour '{repas_type} - {recette_nom} ({date_str})' existe déjà. Ignorée (ou mise à jour si vous implémentez la logique).")
            # Si vous voulez mettre à jour, décommentez et adaptez :
            # update_page_property(existing_page_id, "Participant(s)", "rich_text", str(participants))
        else:
            st.info(f"Création de la page pour '{repas_type} - {recette_nom} ({date_str})'...")
            create_page(database_id_menus, properties)
    st.success("Intégration avec Notion terminée.")
    logger.info("Fin de l'intégration avec Notion.")


def load_data_from_notion():
    """Charge les données des recettes, ingrédients et ingrédients_recettes depuis Notion."""
    st.info("Chargement des données de Recettes, Ingrédients et Relations depuis Notion...")
    df_recettes = pd.DataFrame()
    df_ingredients = pd.DataFrame()
    df_ingredients_recettes = pd.DataFrame()
    id_to_name_map = {} # Pour mapper les IDs aux noms, utile pour les relations

    try:
        # --- Récupération des Recettes ---
        recettes_pages = query_database(DATABASE_ID_RECETTES)
        data_recettes = [get_page_properties(page) for page in recettes_pages]
        df_recettes = pd.DataFrame(data_recettes)

        # Vérification et renommage des colonnes pour correspondre à la logique 'nom', 'participants'
        if 'Nom' in df_recettes.columns and 'Participant(s)' in df_recettes.columns:
            df_recettes.rename(columns={'Nom': 'nom', 'Participant(s)': 'participants'}, inplace=True)
            # Construire un mapping ID -> Nom pour les recettes
            for page in recettes_pages:
                recette_id = page['id']
                recette_name = get_page_properties(page).get('Nom') # Assurez-vous que c'est bien 'Nom'
                if recette_name:
                    id_to_name_map[recette_id] = recette_name
            st.success(f"{len(df_recettes)} recettes chargées depuis Notion.")
        else:
            st.warning("Colonnes 'Nom' ou 'Participant(s)' manquantes dans la base de données Recettes Notion. Veuillez vérifier.")
            df_recettes = pd.DataFrame(columns=['nom', 'participants'])

        # --- Récupération des Ingrédients ---
        ingredients_pages = query_database(DATABASE_ID_INGREDIENTS)
        data_ingredients = [get_page_properties(page) for page in ingredients_pages]
        df_ingredients = pd.DataFrame(data_ingredients)

        # Vérification et renommage des colonnes pour correspondre à 'nom', 'quantite_stock', 'unite'
        # Assurez-vous que 'Nom' est le titre, 'Unité' et 'Quantité en Stock' sont les noms réels dans Notion.
        if 'Nom' in df_ingredients.columns and 'Unité' in df_ingredients.columns and 'Quantité en Stock' in df_ingredients.columns:
            df_ingredients.rename(columns={'Nom': 'nom', 'Unité': 'unite', 'Quantité en Stock': 'quantite_stock'}, inplace=True)
            # Construire un mapping ID -> Nom pour les ingrédients
            for page in ingredients_pages:
                ingredient_id = page['id']
                ingredient_name = get_page_properties(page).get('Nom')
                if ingredient_name:
                    id_to_name_map[ingredient_id] = ingredient_name
            st.success(f"{len(df_ingredients)} ingrédients chargés depuis Notion.")
        else:
            st.warning("Colonnes 'Nom', 'Unité' ou 'Quantité en Stock' manquantes dans la base de données Ingrédients Notion. Veuillez vérifier.")
            df_ingredients = pd.DataFrame(columns=['nom', 'unite', 'quantite_stock'])

        # --- Récupération des relations Ingrédients_recettes ---
        ingredients_recettes_pages = query_database(DATABASE_ID_INGREDIENTS_RECETTES)
        data_ingredients_recettes = []

        # Pour chaque entrée dans la base Ingredients_recettes, nous devons résoudre les relations (IDs vers noms)
        for page in ingredients_recettes_pages:
            props = get_page_properties(page)
            recette_ids = props.get('Recette', []) # Le nom de la propriété de relation vers les recettes
            ingredient_ids = props.get('Ingrédient', []) # Le nom de la propriété de relation vers les ingrédients
            quantite = props.get('Quantité') # Le nom de la propriété numérique pour la quantité
            unite = props.get('Unité') # Le nom de la propriété de sélection ou texte pour l'unité

            # Assurez-vous que les colonnes 'Recette', 'Ingrédient', 'Quantité', 'Unité' existent et sont de type correct dans votre base Notion 'Ingredients_recettes'
            if recette_ids and ingredient_ids and quantite is not None and unite is not None:
                for r_id in recette_ids:
                    recette_name = id_to_name_map.get(r_id)
                    for i_id in ingredient_ids:
                        ingredient_name = id_to_name_map.get(i_id)
                        if recette_name and ingredient_name:
                            data_ingredients_recettes.append({
                                'recette_nom': recette_name,
                                'ingredient_nom': ingredient_name,
                                'quantite': quantite,
                                'unite': unite
                            })
        df_ingredients_recettes = pd.DataFrame(data_ingredients_recettes)
        st.success(f"{len(df_ingredients_recettes)} relations ingrédients-recettes chargées depuis Notion.")

    except Exception as e:
        st.error(f"Erreur lors du chargement des données depuis Notion : {e}")
        logger.error(f"Erreur lors du chargement des données depuis Notion : {e}", exc_info=True)
        # Retourne des DataFrames vides en cas d'erreur
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), {}

    return df_recettes, df_ingredients, df_ingredients_recettes, id_to_name_map


# --- Application Streamlit principale ---
st.set_page_config(layout="wide", page_title="Générateur de Menus Notion")
st.title("🍽️ Générateur de Menus pour Notion")

st.markdown("""
Cette application vous permet de générer des menus, des listes d'ingrédients,
et de les synchroniser avec votre base de données Notion "Planning Menus",
en utilisant vos recettes et ingrédients depuis Notion.
""")

# Étape 1: Charger les données de référence depuis Notion
df_recettes_notion, df_ingredients_notion, df_ingredients_recettes_notion, id_to_name_map = load_data_from_notion()

# Vérifier si le chargement Notion a réussi avant de continuer
if df_recettes_notion.empty or df_ingredients_notion.empty or df_ingredients_recettes_notion.empty:
    st.error("Impossible de charger les données de référence (Recettes, Ingrédients, Relations) depuis Notion. Veuillez vérifier vos bases de données et les noms de colonnes.")
    st.stop() # Arrête l'exécution si les données de base ne sont pas disponibles

st.header("1. Téléchargez votre fichier Planning.csv")
st.info("Ce fichier contient les dates et les repas prévus, avec les noms de recettes que vous voulez planifier.")

uploaded_planning = st.file_uploader("Chargez le fichier Planning.csv", type="csv")

df_planning = None

if uploaded_planning is not None:
    try:
        df_planning = pd.read_csv(uploaded_planning)
        st.success("Planning.csv chargé avec succès.")

        st.header("2. Générer les menus et listes")
        # Le bouton de génération est maintenant activé après le chargement du planning
        if st.button("Générer les Menus et Listes"):
            with st.spinner("Génération et traitement en cours..."):
                df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed = process_data(
                    df_planning, df_recettes_notion, df_ingredients_notion, df_ingredients_recettes_notion, id_to_name_map
                )

                if not df_menus_complet.empty:
                    st.subheader("Aperçu des Menus Générés :")
                    st.dataframe(df_menus_complet[['date', 'repas_type', 'recette_nom', 'Participant(s)']])

                    generate_output_files(df_menus_complet, df_ingredients_processed, df_ingredients_recettes_processed)

                    st.header("3. Intégrer avec Notion")
                    notion_integrate = st.checkbox("Envoyer les menus générés à la base de données Notion 'Planning Menus'?")
                    if notion_integrate:
                        if st.button("Lancer l'intégration Notion"):
                            with st.spinner("Intégration Notion en cours..."):
                                integrate_with_notion(df_menus_complet, DATABASE_ID_MENUS, id_to_name_map)
                                st.success("Processus d'intégration Notion terminé.")
                else:
                    st.warning("Aucun menu n'a pu être généré. Veuillez vérifier votre fichier Planning.csv et vos données Notion.")
    except Exception as e:
        st.error(f"Erreur lors de la lecture ou du traitement de Planning.csv : {e}")
        logger.error(f"Erreur générale avec Planning.csv : {e}", exc_info=True)
else:
    st.info("Veuillez charger le fichier Planning.csv pour activer la génération de menus.")

st.info("N'oubliez pas de configurer vos secrets Notion dans Streamlit Cloud.")
