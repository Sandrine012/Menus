from IPython.display import display, HTML
import pandas as pd
import csv
from notion_client import Client
from notion_client.errors import RequestTimeoutError
import time
import httpx
import logging
import io

# --- Configuration du logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Affichage d'un message visuel bien visible pour le chargement de Planning.csv ---
display(HTML("""
    <div style="border:2px solid #1976D2; border-radius:8px; padding:16px; background:#E3F2FD; margin-bottom:10px;">
        <h3 style="color:#1976D2; margin:0;">
            📄 Veuillez charger le fichier <b>Planning.csv</b> (ou tout fichier contenant "planning" dans le nom) en cliquant sur <u>Choisir des fichiers</u> ci-dessous.
        </h3>
        <p style="color:#333;">Le traitement ne pourra pas continuer sans ce fichier.</p>
    </div>
"""))

# --- Chargement du fichier Planning.csv ---
try:
    from google.colab import files
    uploaded = files.upload()
except ImportError:
    print("⚠️ Ce code doit être exécuté dans Google Colab.")
    # Pour un environnement non-Colab, vous pourriez ajouter une alternative ici,
    # comme demander un chemin de fichier local.
    raise RuntimeError("Ce code nécessite un environnement Google Colab pour la fonction files.upload().")

planning_file = None
for fname in uploaded.keys():
    if 'planning' in fname.lower():
        planning_file = fname
        break

if planning_file is not None:
    print(f"✅ Fichier {planning_file} chargé avec succès !")
    # Détection automatique du séparateur CSV
    # Utiliser io.BytesIO(uploaded[planning_file]) pour lire directement depuis le contenu uploadé
    df_planning = pd.read_csv(io.BytesIO(uploaded[planning_file]), sep=None, engine='python')
    print("Aperçu de Planning.csv :")
    print(df_planning.head())
else:
    print("❌ Aucun fichier contenant 'planning' n'a été chargé. Veuillez réexécuter la cellule et charger le bon fichier.")
    raise RuntimeError("Fichier Planning.csv manquant.")

# --- Paramètres et Fonctions d'extraction de la base de données Notion (Ingrédients) ---
notion = Client(auth="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")
database_id_ingredients = "b23b048b67334032ac1ae4e82d308817" # Renommé pour plus de clarté
csv_filename_ingredients = "Ingredients.csv" # Renommé pour plus de clarté
num_rows_to_extract = 1000  # Limite pour les tests, modifiable

batch_size = 25
api_timeout_seconds = 180
max_retries = 7 # Non utilisé dans l'exemple actuel mais bon à garder

# Fonction pour extraire la valeur d'une propriété Notion
def extract_property_value(prop):
    if not isinstance(prop, dict):
        return ""
    t = prop.get("type")
    if t == "title":
        return "".join([t.get("plain_text", "") for t in prop.get("title", [])])
    elif t == "rich_text":
        return "".join([t.get("plain_text", "") for t in prop.get("rich_text", [])])
    elif t == "multi_select":
        return ", ".join([opt.get("name", "") for opt in prop.get("multi_select", [])])
    elif t == "select":
        select_obj = prop.get("select")
        if select_obj is not None:
            return select_obj.get("name", "")
        return ""
    elif t == "number":
        return str(prop.get("number", ""))
    elif t == "checkbox":
        return str(prop.get("checkbox", ""))
    elif t == "date":
        date_obj = prop.get("date")
        if date_obj is not None:
            return date_obj.get("start", "")
        return ""
    elif t == "people":
        return ", ".join([person.get("name", "") for person in prop.get("people", [])])
    elif t == "relation":
        # Pour les relations, souvent on veut les IDs ou faire une requête secondaire
        # Ici, on extrait juste les IDs pour le CSV
        return ", ".join([rel.get("id", "") for rel in prop.get("relation", [])])
    elif t == "url":
        return prop.get("url", "")
    elif t == "email":
        return prop.get("email", "")
    elif t == "phone_number":
        return prop.get("phone_number", "")
    elif t == "formula":
        formula = prop.get("formula", {})
        if formula.get("type") == "string":
            return formula.get("string", "")
        elif formula.get("type") == "number":
            return str(formula.get("number", ""))
        elif formula.get("type") == "boolean":
            return str(formula.get("boolean", ""))
        elif formula.get("type") == "date":
            date_obj = formula.get("date")
            if date_obj is not None:
                return date_obj.get("start", "")
            return ""
    elif t == "rollup":
        rollup = prop.get("rollup", {})
        if rollup.get("type") == "array":
            # Gérer les rollups qui sont des tableaux d'éléments
            # Cela peut nécessiter une logique plus complexe selon le contenu des éléments
            return ", ".join([
                str(item.get("plain_text", "") or item.get("number", "") or "") # Exemple simplifié
                for item in rollup.get("array", [])
            ])
        elif rollup.get("type") in ["number", "string", "boolean", "date"]:
            # Accéder directement à la valeur si c'est un type simple
            return str(rollup.get(rollup.get("type"), ""))
    return ""

# --- Extraction des données de la base Notion et écriture dans Ingredients.csv ---
total_extracted = 0
next_cursor = None

try:
    with open(csv_filename_ingredients, 'w', newline='', encoding='utf-8') as csvfile:
        csv_writer = None
        header_written = False # Nouveau flag pour s'assurer que l'en-tête est écrit une seule fois

        while total_extracted < num_rows_to_extract:
            try:
                # Requête Notion
                results = notion.databases.query(
                    database_id=database_id_ingredients,
                    start_cursor=next_cursor,
                    page_size=batch_size,
                    timeout=api_timeout_seconds,
                    filter={
                        "property": "Type de stock",
                        "select": {"equals": "Autre type"}
                    }
                )
                page_results = results.get("results", [])

                if not page_results:
                    logger.info("Aucun résultat retourné par l'API ou fin de la base de données atteinte.")
                    break

                if not header_written:
                    # Définir l'en-tête une seule fois
                    header = ["Page_ID", "Nom", "Type de stock", "unité", "Qte reste"]
                    csv_writer = csv.writer(csvfile, quoting=csv.QUOTE_MINIMAL, dialect='excel')
                    csv_writer.writerow(header)
                    header_written = True

                for result in page_results:
                    if total_extracted >= num_rows_to_extract:
                        break # Arrêter si le nombre maximum de lignes est atteint

                    properties = result.get("properties", {})
                    row_values = [result.get("id", "")]
                    
                    # Extraire les valeurs pour chaque colonne de l'en-tête
                    row_values.append(extract_property_value(properties.get("Nom", {})))
                    row_values.append(extract_property_value(properties.get("Type de stock", {})))
                    row_values.append(extract_property_value(properties.get("unité", {})))
                    row_values.append(extract_property_value(properties.get("Qte reste", {})))
                    
                    csv_writer.writerow(row_values)
                    total_extracted += 1

                next_cursor = results.get("next_cursor")
                if not next_cursor:
                    break # Plus de pages à charger
                
                time.sleep(1) # Respecter les limites de débit de l'API Notion

            except (httpx.TimeoutException, RequestTimeoutError) as e:
                logger.warning(f"Timeout détecté lors de la requête Notion : {e}. Réessai...")
                time.sleep(10) # Attendre avant de réessayer après un timeout
                continue
            except Exception as e:
                logger.exception(f"Erreur inattendue lors de l'extraction Notion : {e}")
                break # Arrêter en cas d'erreur inattendue
    
    logger.info(f"Extraction terminée. {total_extracted} lignes exportées dans {csv_filename_ingredients}.")
    print(f"✅ Fichier '{csv_filename_ingredients}' créé avec succès !")

except IOError as e:
    logger.error(f"Erreur d'écriture du fichier '{csv_filename_ingredients}' : {e}")
    print(f"❌ Erreur : Impossible d'écrire le fichier '{csv_filename_ingredients}'. {e}")

# Vous pouvez maintenant lire df_planning et le fichier Ingredients.csv généré (df_ingredients)
# pour continuer le traitement dans les étapes suivantes de votre Colab.

# Exemple de lecture du fichier Ingredients.csv généré :
try:
    df_ingredients = pd.read_csv(csv_filename_ingredients)
    print("\nAperçu de Ingredients.csv (extrait de Notion) :")
    print(df_ingredients.head())
except FileNotFoundError:
    print(f"Le fichier {csv_filename_ingredients} n'a pas été trouvé. Il pourrait y avoir eu une erreur lors de l'extraction.")
except Exception as e:
    print(f"Erreur lors de la lecture de {csv_filename_ingredients} : {e}")
