import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta

# Configuration du logger pour Streamlit
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

# Constantes globales
NB_JOURS_ANTI_REPETITION = 42
COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID"  # Utilisé comme ID pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID"  # Utilisé comme ID pour Ingredients
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
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}")

class RecetteManager:
    """Gère l'accès et les opérations sur les données de recettes et ingrédients."""

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
            logger.error("'Qte reste' manquante dans df_ingredients pour stock_simule.")
            self.stock_simule["Qte reste"] = 0.0

        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve()

    def get_ingredients_for_recipe(self, recette_id_str):
        try:
            recette_id_str = str(recette_id_str)
            ingredients = self.df_ingredients_recettes[
                self.df_ingredients_recettes[COLONNE_ID_RECETTE].astype(str) == recette_id_str
            ][["Ingrédient ok", "Qté/pers_s"]].to_dict('records')
            return ingredients
        except Exception as e:
            logger.error(f"Erreur récupération ingrédients pour {recette_id_str} : {e}")
            return []

    def _trouver_ingredients_stock_eleve(self):
        seuil_gr = 100
        seuil_pc = 1
        ingredients_stock = {}
        cols_attendues = ["Qte reste", "unité", COLONNE_ID_INGREDIENT, "Nom"]
        if not all(col in self.stock_simule.columns for col in cols_attendues):
            logger.warning("Colonnes manquantes dans stock_simule pour _trouver_ingredients_stock_eleve.")
            return {}
        for _, row in self.stock_simule.iterrows():
            try:
                qte = float(str(row["Qte reste"]).replace(",", "."))
                unite = str(row["unité"]).lower()
                page_id = str(row[COLONNE_ID_INGREDIENT])
                if (unite in ["gr", "g", "ml", "cl"] and qte >= seuil_gr) or (unite in ["pc", "tranches"] and qte >= seuil_pc):
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
            logger.error(f"Erreur dans recette_utilise_ingredient_anti_gaspi pour {recette_id_str} : {e}")
            return False

    def calculer_quantite_necessaire(self, recette_id_str, nb_personnes):
        ingredients_necessaires = {}
        try:
            ingredients_recette = self.get_ingredients_for_recipe(recette_id_str)
            if not ingredients_recette:
                return {}
            for ing in ingredients_recette:
                try:
                    ing_id = str(ing.get("Ingrédient ok"))
                    if not ing_id or ing_id.lower() in ['nan', 'none', '']:
                        continue
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


    def obtenir_temps_preparation(self, recette_page_id_str):
        try:
            recette_page_id_str = str(recette_page_id_str)
            if self.df_recettes.index.name == COLONNE_ID_RECETTE:
                recette_info = self.df_recettes.loc[recette_page_id_str]
            else:
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str].iloc[0]
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

    def obtenir_nom_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            nom = self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Nom'].iloc[0]
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
                recette_info = self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str].iloc[0]

            if COLONNE_AIME_PAS_PRINCIP not in recette_info or pd.isna(recette_info[COLONNE_AIME_PAS_PRINCIP]):
                return True
            n_aime_pas = [code.strip() for code in str(recette_info[COLONNE_AIME_PAS_PRINCIP]).split(",") if code.strip()]
            participants_actifs = [code.strip() for code in participants_str_codes.split(",") if code.strip()]
            is_adapted = not any(code_participant in n_aime_pas for code_participant in participants_actifs)

            if not is_adapted:
                logger.debug(f"Recette {self.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée par participants. Participants actifs: {participants_actifs}, N'aime pas: {n_aime_pas}")
            return is_adapted
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
                valeur = str(self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str]["Transportable"].iloc[0]).strip().lower()
            is_transportable = (valeur == "oui")
            if not is_transportable:
                logger.debug(f"Recette {self.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée: Non transportable (valeur: '{valeur}')")
            return is_transportable
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour transportable.")
            return False
        except Exception as e:
            logger.error(f"Erreur vérification transportable pour {recette_page_id_str}: {e}")
            return False
    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str): 
            return 1
        if participants_str_codes == "B":
            return 1
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def generer_menu_repas_b(self, date_repas, plats_transportables_semaine_dict, repas_b_utilises_ids_list, menu_recent_noms_list):
        """
        Génère un repas de type B (reste) en réutilisant un plat transportable cuisiné dans les 2 jours précédents.
        """
        candidats_restes_ids = []

        # Formatage date simple (sans heure) pour comparaison
        date_repas_simple = date_repas.date() if hasattr(date_repas, "date") else date_repas

        # Affichage du pool avant sélection
        print(f"--- Pool plats_transportables_semaine avant repas B du {date_repas_simple} ---")
        for date_clef, recette_id in plats_transportables_semaine_dict.items():
            date_str = date_clef.strftime('%Y-%m-%d') if hasattr(date_clef, 'strftime') else str(date_clef)
            print(f"  {date_str} => recette id: {recette_id}")
        print("-------------------------------------")

        # Trie les plats par date (le plus ancien en premier)
        sorted_plats_transportables = sorted(plats_transportables_semaine_dict.items(), key=lambda item: item[0])

        for date_plat_orig, plat_id_orig_str in sorted_plats_transportables:
            # S'assurer que date_plat_orig est un date (sans heure)
            if hasattr(date_plat_orig, "date"):
                date_plat_orig = date_plat_orig.date()

            jours_ecoules = (date_repas_simple - date_plat_orig).days

            nom_plat_reste = self.recette_manager.obtenir_nom(plat_id_orig_str)
            logger.debug(f"Évaluation reste {nom_plat_reste} (ID: {plat_id_orig_str}) du {date_plat_orig}. Jours écoulés: {jours_ecoules}.")

            # Conditions strictes : cuisiné dans les 2 jours précédents (1 ou 2), pas 0 ni > 2
            if not (0 < jours_ecoules <= 2):
                logger.debug(f"Reste {nom_plat_reste} filtré : jours écoulés ({jours_ecoules}) hors plage 1-2")
                continue

            if plat_id_orig_str in repas_b_utilises_ids_list:
                logger.debug(f"Reste {nom_plat_reste} filtré : déjà utilisé dans un repas B")
                continue

            if not (nom_plat_reste and nom_plat_reste.strip() and "Recette_ID_" not in nom_plat_reste):
                logger.debug(f"Reste {nom_plat_reste} filtré : nom invalide ou générique")
                continue

            if not self.recette_manager.est_transportable(plat_id_orig_str):
                logger.debug(f"Reste {nom_plat_reste} filtré : recette non transportable dans Recettes.csv")
                continue

            # Pas de filtrage anti-répétition ici pour les restes (repas B)
            candidats_restes_ids.append(plat_id_orig_str)
            logger.debug(f"Reste {nom_plat_reste} ajouté aux candidats restes")

        if candidats_restes_ids:
            # Choix du premier (plus ancien)
            plat_id_choisi_str = candidats_restes_ids[0]
            nom_plat_choisi_str = self.recette_manager.obtenir_nom(plat_id_choisi_str)
            repas_b_utilises_ids_list.append(plat_id_choisi_str)

            logger.info(f"Reste choisi pour Repas B: {nom_plat_choisi_str} (ID: {plat_id_choisi_str})")
            return f"Restes : {nom_plat_choisi_str}", plat_id_choisi_str, "Reste transportable utilisé"

        logger.info("Pas de reste disponible trouvé pour ce Repas B")
        return "Pas de reste disponible", None, "Aucun reste transportable trouvé"

    def generer_menu(self):
        resultats_df_list = []
        repas_b_utilises_ids = []
        plats_transportables_semaine = {}  # Pool des plats transportables cuisinés récemment
        used_recipes_current_generation_set = set()
        menu_recent_noms = []
        self.ingredients_a_acheter_cumules = {}

        for _, repas_planning_row in self.df_planning.sort_values("Date").iterrows():
            date_repas_dt = repas_planning_row["Date"]
            participants_str = str(repas_planning_row["Participants"])
            participants_count = self.compter_participants(participants_str)
            transportable_req = str(repas_planning_row.get("Transportable", "")).strip().lower()
            temps_req = str(repas_planning_row.get("Temps", "")).strip().lower()
            nutrition_req = str(repas_planning_row.get("Nutrition", "")).strip().lower()

            logger.info(f"\n--- Traitement Planning: {date_repas_dt.strftime('%d/%m/%Y %H:%M')} - Participants: {participants_str} ---")

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = ""
            temps_prep_final = 0
            ingredients_consommes_ce_repas = []
            ingredients_manquants_pour_recette_choisie = {}

            if participants_str == "B":
                # Repas B: cherche reste valable
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, 1, date_repas_dt)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
            else:
                # Repas standard
                recette_choisie_id, ingredients_manquants_pour_recette_choisie = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count, used_recipes_current_generation_set,
                    menu_recent_noms, transportable_req, temps_req, nutrition_req
                )

                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    remarques_repas = "Généré automatiquement"
                else:
                    nom_plat_final = "Recette non trouvée"
                    remarques_repas = "Aucune recette appropriée trouvée selon les critères."

                if recette_choisie_id:
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                    used_recipes_current_generation_set.add(recette_choisie_id)

                    # Correction clé : stocke avec clé date (sans heure)
                    if participants_str != "B" and self.recette_manager.est_transportable(recette_choisie_id):
                        cle_date = date_repas_dt.date() if hasattr(date_repas_dt, "date") else date_repas_dt
                        plats_transportables_semaine[cle_date] = recette_choisie_id
                        logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) ajouté à plats_transportables_semaine pour le {cle_date}.")

            # Mise à jour des ingrédients manquants cumulés
            for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                current_qte = self.ingredients_a_acheter_cumules.get(ing_id, 0.0)
                self.ingredients_a_acheter_cumules[ing_id] = current_qte + qte_manquante
                logger.debug(f"Ingrédient manquant cumulé : {self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)} - {qte_manquante:.2f} (total: {self.ingredients_a_acheter_cumules[ing_id]:.2f})")

            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)

            self._ajouter_resultat(
                resultats_df_list, date_repas_dt, nom_plat_final, participants_str,
                remarques_repas, temps_prep_final, recette_choisie_id
            )

            # Anti-répétition : mémorisation des noms récents (hors repas B)
            if nom_plat_final and "Pas de recette" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Erreur" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3:
                    menu_recent_noms.pop(0)

        df_menu_genere = pd.DataFrame(resultats_df_list)

        liste_courses_final = {}

        for ing_id, qte_cumulee in self.ingredients_a_acheter_cumules.items():
            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            if nom_ing and "ID_Ing_" not in nom_ing:
                unite_ing = "unité(s)"
                try:
                    unite_ing_df = self.recette_manager.df_ingredients_initial[
                        self.recette_manager.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_id
                    ]
                    if not unite_ing_df.empty and 'unité' in unite_ing_df.columns:
                        unite_ing = unite_ing_df['unité'].iloc[0]
                except Exception as e:
                    logger.warning(f"Impossible de récupérer l'unité pour l'ingrédient {nom_ing} : {e}")

                liste_courses_final[nom_ing] = f"{qte_cumulee:.2f} {unite_ing}"
            else:
                liste_courses_final[f"ID Ingrédient {ing_id}"] = f"{qte_cumulee:.2f} unité(s) (Nom non trouvé)"

        if not df_menu_genere.empty:
            logger.info(f"Nombre de lignes totales générées : {len(df_menu_genere)}")
            if 'Date' in df_menu_genere.columns:
                df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'], format="%d/%m/%Y %H:%M", errors='coerce').dt.strftime('%Y-%m-%d %H:%M')

        formatted_liste_courses = []
        for ing, qte_unite in liste_courses_final.items():
            formatted_liste_courses.append(f"{ing}: {qte_unite}")
        formatted_liste_courses.sort()

        return df_menu_genere, formatted_liste_courses
# Suite et fin des méthodes et de l'interface

# --- Compléments possibles pour la gestion des ingrédients, interface, utils etc. ---

# Note : Les principaux composants pour générer les menus, gérer les restes en "repas B"
# et stocker les plats transportables avec la clef 'date' sans heure sont inclus précédemment.

# Si tu as d'autres fonctions spécifiques ou UI Streamlit avancée,
# ajoute-les ici en respectant la structure.

# -------------------------------------------
# Fonction principale appelée par Streamlit
def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus et Liste de Courses")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Veuillez charger tous les fichiers CSV nécessaires.")

    uploaded_files = {}
    file_names = ["Recettes.csv", "Planning.csv", "Menus.csv", "Ingredients.csv", "Ingredients_recettes.csv"]
    for file_name in file_names:
        uploaded_files[file_name] = st.sidebar.file_uploader(f"Uploader {file_name}", type="csv", key=file_name)

    dataframes = {}
    all_files_uploaded = True

    for file_name, uploaded_file in uploaded_files.items():
        if uploaded_file is not None:
            try:
                # Lecture avec gestion possible du séparateur ';' pour Planning.csv
                if file_name == "Planning.csv":
                    uploaded_file.seek(0)
                    df_tmp = pd.read_csv(uploaded_file, encoding='utf-8')
                    if len(df_tmp.columns) == 1 and ';' in df_tmp.iloc[0, 0]:
                        uploaded_file.seek(0)
                        df = pd.read_csv(uploaded_file, encoding='utf-8', sep=';')
                    else:
                        df = df_tmp
                else:
                    df = pd.read_csv(uploaded_file, encoding='utf-8')

                # Nettoyage et conversions si nécessaire
                if "Temps_total" in df.columns:
                    df["Temps_total"] = pd.to_numeric(df["Temps_total"], errors='coerce').fillna(VALEUR_DEFAUT_TEMPS_PREPARATION).astype(int)

                if "Calories" in df.columns:
                    df["Calories"] = pd.to_numeric(df["Calories"], errors='coerce')

                if "Proteines" in df.columns:
                    df["Proteines"] = pd.to_numeric(df["Proteines"], errors='coerce')

                dataframes[file_name.replace(".csv", "")] = df
                st.sidebar.success(f"{file_name} chargé avec succès.")

            except Exception as e:
                st.sidebar.error(f"Erreur lors du chargement de {file_name}: {e}")
                all_files_uploaded = False
                break
        else:
            all_files_uploaded = False
            break

    if not all_files_uploaded:
        st.warning("Veuillez charger tous les fichiers CSV pour continuer.")
        return

    # Vérification des colonnes attendues
    try:
        verifier_colonnes(dataframes["Recettes"], [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories", "Proteines"], "Recettes.csv")
        verifier_colonnes(dataframes["Planning"], ["Date", "Participants", "Transportable", "Temps", "Nutrition"], "Planning.csv")
        verifier_colonnes(dataframes["Menus"], ["Date", "Recette"], "Menus.csv")
        verifier_colonnes(dataframes["Ingredients"], [COLONNE_ID_INGREDIENT, "Nom", "Qte reste", "unité"], "Ingredients.csv")
        verifier_colonnes(dataframes["Ingredients_recettes"], [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingredients_recettes.csv")
    except ValueError:
        st.error("Des colonnes essentielles sont manquantes dans un ou plusieurs fichiers. Veuillez vérifier les en-têtes de vos fichiers CSV.")
        return

    st.markdown("---")
    st.header("1. Générer le Menu")
    st.write("Cliquez sur le bouton ci-dessous pour générer le menu hebdomadaire et la liste de courses.")

    if st.button("🚀 Générer le Menu"):
        with st.spinner("Génération du menu en cours... Cela peut prendre quelques instants."):
            try:
                menu_generator = MenuGenerator(
                    dataframes["Menus"],
                    dataframes["Recettes"],
                    dataframes["Planning"],
                    dataframes["Ingredients"],
                    dataframes["Ingredients_recettes"]
                )
                df_menu_genere, liste_courses = menu_generator.generer_menu()

                st.success("🎉 Menu généré avec succès !")

                st.header("2. Menu Généré")
                st.dataframe(df_menu_genere)

                st.header("3. Liste de Courses (Ingrédients manquants cumulés)")
                if liste_courses:
                    liste_courses_df = pd.DataFrame({"Ingrédient et Quantité": liste_courses})
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

