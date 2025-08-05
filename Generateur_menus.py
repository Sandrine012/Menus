import streamlit as st
import pandas as pd
import random
import logging
from datetime import datetime, timedelta
import time, httpx
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ────── CONFIGURATION INITIALE ──────────────────────────────────
# Configuration du logger pour Streamlit
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

# ────── AJOUT DES DÉPENDANCES NOTION ───────────────────────────
NOTION_API_KEY           = st.secrets["notion_api_key"]
ID_MENUS                 = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS           = st.secrets["notion_database_id_ingredients"]
BATCH_SIZE, MAX_RETRY, WAIT_S = 50, 3, 5

notion = Client(auth=NOTION_API_KEY)

# ────── AJOUT DES FONCTIONS D'EXTRACTION NOTION ─────────────────
def paginate(db_id, **kwargs):
    out, cur, retry = [], None, 0
    while True:
        try:
            resp = notion.databases.query(database_id=db_id,
                                          start_cursor=cur,
                                          page_size=BATCH_SIZE,
                                          **kwargs)
            out.extend(resp["results"])
            if not resp["has_more"]:
                break
            cur = resp["next_cursor"]
            time.sleep(0.3)
            retry = 0
        except (RequestTimeoutError, httpx.TimeoutException, httpx.ReadTimeout):
            retry += 1
            if retry > MAX_RETRY:
                st.error("Timeout répété – arrêt.")
                break
            time.sleep(WAIT_S * retry)
        except APIResponseError as e:
            st.error(f"Erreur API : {e}")
            break
    return out

HDR_MENUS = ["Nom Menu","Recette","Date"]
def extract_menus():
    rows=[]
    for p in paginate(ID_MENUS,
            filter={"property":"Recette","relation":{"is_not_empty":True}}):
        pr = p["properties"]
        nom = "".join(t["plain_text"] for t in pr["Nom Menu"]["title"])
        rec_ids=[]
        rel=pr["Recette"]
        if rel["type"]=="relation":
            rec_ids=[r["id"] for r in rel["relation"]]
        else:
            for it in rel["rollup"]["array"]:
                rec_ids.extend([it.get("id")] if it.get("id") else
                               [r["id"] for r in it.get("relation",[])])
        d=""
        if pr["Date"]["date"] and pr["Date"]["date"]["start"]:
            d=datetime.fromisoformat(pr["Date"]["date"]["start"].replace("Z","+00:00")).strftime("%Y-%m-%d")
        rows.append([nom.strip(), ", ".join(rec_ids), d])
    return pd.DataFrame(rows,columns=HDR_MENUS)

# NOUVEAU : Fonction pour extraire les données des ingrédients depuis Notion
HDR_INGREDIENTS = [COLONNE_ID_INGREDIENT, "Nom", "unité", "Qte reste"]
def extract_ingredients():
    rows = []
    for p in paginate(ID_INGREDIENTS):
        pr = p["properties"]
        page_id = p["id"]
        nom = "".join(t["plain_text"] for t in pr["Nom"]["title"])
        unite = pr["unité"]["select"]["name"] if pr["unité"]["select"] else ""
        
        # Correction pour gérer les valeurs vides de la colonne "Qte reste"
        qte_reste = pr["Qte reste"]["number"] if pr["Qte reste"] and "number" in pr["Qte reste"] and pr["Qte reste"]["number"] is not None else 0
        
        rows.append([page_id, nom.strip(), unite.strip(), qte_reste])
    return pd.DataFrame(rows, columns=HDR_INGREDIENTS)

# ────── FIN DES FONCTIONS D'EXTRACTION ───────────────────────────


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
        if COLONNE_ID_INGREDIENT in self.df_ingredients_initial.columns:
            self.df_ingredients_initial = self.df_ingredients_initial.set_index(COLONNE_ID_INGREDIENT, drop=False)

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
        if not all(col in self.stock_simule.columns for col in ["Qte reste", "unité", COLONNE_ID_INGREDIENT, "Nom"]):
            logger.warning("Colonnes manquantes dans stock_simule pour _trouver_ingredients_stock_eleve.")
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
            logger.error(f"Erreur dans recette_utilise_ingredient_anti_gaspi pour {recette_id_str} : {e}")
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
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        if not ingredients_necessaires: return 0, 0, {}

        total_ingredients_definis = len(ingredients_necessaires)
        ingredients_disponibles_compteur = 0
        score_total_dispo = 0
        ingredients_manquants = {}

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            ing_stock_df = self.stock_simule[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id_str]

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

        pourcentage_dispo = (ingredients_disponibles_compteur / total_ingredients_definis) * 100 if total_ingredients_definis > 0 else 0
        score_moyen_dispo = score_total_dispo / total_ingredients_definis if total_ingredients_definis > 0 else 0

        logger.debug(f"Éval recette {recette_id_str}: Score={score_moyen_dispo:.2f}, %Dispo={pourcentage_dispo:.0f}%")
        return score_moyen_dispo, pourcentage_dispo, ingredients_manquants

    def decrementer_stock(self, recette_id_str, nb_personnes, date_repas):
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        ingredients_consommes_ids = set()

        for ing_id, qte_necessaire in ingredients_necessaires.items():
            ing_id_str = str(ing_id)
            idx_list = self.stock_simule.index[self.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == ing_id_str].tolist()
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
                return self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str][COLONNE_NOM].iloc[0]
        except (KeyError, IndexError):
            logger.warning(f"Recette ID {recette_page_id_str} non trouvé dans df_recettes (obtenir_nom).")
            return f"Recette_ID_{recette_page_id_str}"
        except Exception as e:
            logger.error(f"Erreur obtenir_nom pour recette ID {recette_page_id_str}: {e}")
            return None

    def obtenir_nom_ingredient_par_id(self, ing_page_id_str):
        try:
            ing_page_id_str = str(ing_page_id_str)
            if self.df_ingredients_initial.index.name == COLONNE_ID_INGREDIENT:
                 return self.df_ingredients_initial.loc[ing_page_id_str, 'Nom']
            else:
                return self.df_ingredients_initial.loc[self.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_page_id_str, 'Nom'].iloc[0]
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

class MenusHistoryManager:
    """Gère l'accès et les opérations sur l'historique des menus."""
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()
        self.df_menus_historique["Date"] = pd.to_datetime(self.df_menus_historique["Date"], errors="coerce")
        self.df_menus_historique.dropna(subset=["Date"], inplace=True)
        if 'Date' in self.df_menus_historique.columns:
            self.df_menus_historique['Semaine'] = self.df_menus_historique['Date'].dt.isocalendar().week
        else:
            logger.warning("La colonne 'Date' est manquante dans l'historique des menus, impossible de calculer la semaine.")

class MenuGenerator:
    """Génère les menus en fonction du planning et des règles."""
    def __init__(self, df_menus_hist, df_recettes, df_planning, df_ingredients, df_ingredients_recettes):
        self.df_planning = df_planning.copy()
        if "Date" in self.df_planning.columns:
            self.df_planning['Date'] = pd.to_datetime(self.df_planning['Date'], errors='coerce')
            self.df_planning.dropna(subset=['Date'], inplace=True)
        else:
            logger.error("'Date' manquante dans le planning.")
            raise ValueError("Colonne 'Date' manquante dans le fichier de planning.")

        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)
        self.ingredients_a_acheter_cumules = {}

    def recettes_meme_semaine_annees_precedentes(self, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Semaine', 'Recette']):
                return set()

            semaine_actuelle = date_actuelle.isocalendar()[1]
            annee_actuelle = date_actuelle.year

            df_menus_semaine = df_hist[
                (df_hist["Semaine"].astype(int) == semaine_actuelle) &
                (df_hist["Date"].dt.year < annee_actuelle) &
                pd.notna(df_hist["Recette"])
            ]
            return set(df_menus_semaine["Recette"].astype(str).unique())
        except Exception as e:
            logger.error(f"Erreur recettes_meme_semaine_annees_precedentes pour {date_actuelle}: {e}")
            return set()

    def est_recente(self, recette_page_id_str, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Recette']):
                return False

            debut = date_actuelle - timedelta(days=NB_JOURS_ANTI_REPETITION)
            fin = date_actuelle + timedelta(days=NB_JOURS_ANTI_REPETITION)
            mask = (
                (df_hist['Recette'].astype(str) == str(recette_page_id_str)) &
                (df_hist['Date'] >= debut) &
                (df_hist['Date'] <= fin)
            )
            is_recent = not df_hist.loc[mask].empty
            if is_recent:
                logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} ({recette_page_id_str}) filtrée: Est récente (dans les {NB_JOURS_ANTI_REPETITION} jours)")
            return is_recent
        except Exception as e:
            logger.error(f"Erreur est_recente pour {recette_page_id_str} à {date_actuelle}: {e}")
            return False

    def trouver_restes_transportables(self, date_repas, used_recipes_in_current_gen):
        """Trouve une recette transportable cuisinée dans les 2 jours précédents."""
        df_hist = self.menus_history_manager.df_menus_historique
        if df_hist.empty:
            logger.debug("Historique des menus vide.")
            return None
        
        # Le bug était ici. Il faut un `timedelta` pour remonter le temps.
        deux_jours_avant = date_repas - timedelta(days=2)
        
        recettes_candidates_ids = set()
        
        # Filtre sur l'historique des menus pour les dates et les recettes non vides
        recent_menus = df_hist[
            (df_hist['Date'] >= deux_jours_avant) &
            (df_hist['Date'] < date_repas) &
            df_hist['Recette'].notna()
        ]

        if recent_menus.empty:
            logger.debug(f"Aucune recette récente transportable trouvée pour le {date_repas.strftime('%Y-%m-%d')}")
            return None
            
        for recette_id in recent_menus['Recette'].unique():
            recette_id_str = str(recette_id).strip()
            if self.recette_manager.est_transportable(recette_id_str):
                recettes_candidates_ids.add(recette_id_str)
        
        # Filtre les recettes déjà utilisées dans la génération de menu actuelle
        recettes_candidates_ids = list(recettes_candidates_ids - used_recipes_in_current_gen)

        if not recettes_candidates_ids:
            logger.debug(f"Aucune recette de reste transportable trouvée parmi les repas récents pour le {date_repas.strftime('%Y-%m-%d')}.")
            return None

        recette_choisie_id = random.choice(recettes_candidates_ids)
        logger.debug(f"Recette de reste transportable choisie : {self.recette_manager.obtenir_nom(recette_choisie_id)}")
        return recette_choisie_id


    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str):
            return 1
        if participants_str_codes.strip().upper() == "B":
            return 1
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def _filtrer_recette_base(self, recette_id_str, participants_str_codes):
        return self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)

    def generer_recettes_candidates(self, date_repas, participants_str_codes, used_recipes_in_current_gen, transportable_req, temps_req, nutrition_req):
        candidates = []
        anti_gaspi_candidates = []
        recettes_scores_dispo = {}
        recettes_ingredients_manquants = {}
        nb_personnes = self.compter_participants(participants_str_codes)
        logger.debug(f"--- Recherche de candidats pour {date_repas.strftime('%Y-%m-%d %H:%M')} (Participants: {participants_str_codes}) ---")
        for recette_id_str_cand in self.recette_manager.df_recettes.index.astype(str):
            nom_recette_cand = self.recette_manager.obtenir_nom(recette_id_str_cand)
            if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Non transportable pour une demande transportable.")
                continue
            temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
            if temps_req == "express" and temps_total > TEMPS_MAX_EXPRESS:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Express ({TEMPS_MAX_EXPRESS} min).")
                continue
            if temps_req == "rapide" and temps_total > TEMPS_MAX_RAPIDE:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Temps ({temps_total} min) > Rapide ({TEMPS_MAX_RAPIDE} min).")
                continue
            if recette_id_str_cand in used_recipes_in_current_gen:
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Déjà utilisé dans la génération actuelle.")
                continue
            if not self._filtrer_recette_base(recette_id_str_cand, participants_str_codes):
                continue
            
            score_dispo, pourcentage_dispo, ingredients_manquants = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_cand, nb_personnes)
            
            recettes_scores_dispo[recette_id_str_cand] = score_dispo
            recettes_ingredients_manquants[recette_id_str_cand] = ingredients_manquants

            is_anti_gaspi = self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand)
            if is_anti_gaspi:
                anti_gaspi_candidates.append(recette_id_str_cand)
                
            candidates.append(recette_id_str_cand)
            
        return candidates, anti_gaspi_candidates, recettes_scores_dispo, recettes_ingredients_manquants

    def generer_menu_planifie(self, df_recettes, df_ingredients_recettes):
        planning_genere = []
        used_recipes = set()
        
        df_menus_hist = st.session_state.get('df_menus_hist', pd.DataFrame(columns=HDR_MENUS))
        df_ingredients = st.session_state.get('df_ingredients')

        recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        menus_history_manager = MenusHistoryManager(df_menus_hist)
        
        for index, row in self.df_planning.iterrows():
            date_repas = row["Date"]
            participants = row.get("Participants", "")
            type_repas = row.get("Type_de_repas", "")
            
            recette_choisie_id = "Pas de recette"
            nom_recette = "Pas de recette"
            recettes_scores_dispo = {}
            recettes_ingredients_manquants = {}

            # Logique spécifique pour les "Restes"
            if participants.strip().upper() == "B":
                recette_choisie_id = self.trouver_restes_transportables(date_repas, used_recipes)
                if recette_choisie_id:
                    nom_recette = f"Restes : {self.recette_manager.obtenir_nom(recette_choisie_id)}"
                    used_recipes.add(recette_choisie_id)
                else:
                    nom_recette = "Pas de restes transportables récents"
            else:
                recettes_candidates, anti_gaspi_candidates, recettes_scores_dispo, recettes_ingredients_manquants = self.generer_recettes_candidates(
                    date_repas,
                    participants,
                    used_recipes,
                    row.get("Transportable", ""),
                    row.get("Temps", ""),
                    row.get("Nutrition", "")
                )
                
                if anti_gaspi_candidates:
                    recette_choisie_id = random.choice(anti_gaspi_candidates)
                elif recettes_candidates:
                    recette_choisie_id = random.choice(recettes_candidates)
                
                if recette_choisie_id != "Pas de recette":
                    used_recipes.add(recette_choisie_id)
                    self.recette_manager.decrementer_stock(recette_choisie_id, self.compter_participants(participants), date_repas)
                
                nom_recette = self.recette_manager.obtenir_nom(recette_choisie_id) if recette_choisie_id != "Pas de recette" else "Pas de recette"
            
            planning_genere.append({
                "Date": date_repas,
                "Type_de_repas": type_repas,
                "Recette": nom_recette,
                "Recette_ID": recette_choisie_id,
                "Score_Disponibilite": recettes_scores_dispo.get(recette_choisie_id, 0),
                "Ingredients_manquants": recettes_ingredients_manquants.get(recette_choisie_id, {})
            })

        return pd.DataFrame(planning_genere)

# ────── LOGIQUE PRINCIPALE DE L'APPLICATION STREAMLIT ───────────
st.set_page_config(layout="wide")
st.title("Générateur de Menus Automatisé")

# Initialisation de st.session_state
if 'df_menus_hist' not in st.session_state:
    st.session_state['df_menus_hist'] = None
if 'df_recipes' not in st.session_state:
    st.session_state['df_recipes'] = None
if 'df_ingredients_recipes' not in st.session_state:
    st.session_state['df_ingredients_recipes'] = None
if 'df_ingredients' not in st.session_state:
    st.session_state['df_ingredients'] = None
if 'df_planning' not in st.session_state:
    st.session_state['df_planning'] = None

with st.sidebar:
    st.header("Chargement des données")
    uploaded_file_planning = st.file_uploader("1. Charger le planning (Planning.csv)", type="csv")
    uploaded_file_recettes = st.file_uploader("2. Charger les recettes (Recettes.csv)", type="csv")
    uploaded_file_ingr_recettes = st.file_uploader("3. Charger les ingrédients des recettes (Ingredients_recettes.csv)", type="csv")

    if st.button("4. Charger les données Notion & CSV"):
        with st.spinner("Chargement en cours..."):
            try:
                st.session_state['df_ingredients'] = extract_ingredients()
                if uploaded_file_planning:
                    st.session_state['df_planning'] = pd.read_csv(uploaded_file_planning, sep=';')
                if uploaded_file_recettes:
                    st.session_state['df_recipes'] = pd.read_csv(uploaded_file_recettes)
                if uploaded_file_ingr_recettes:
                    st.session_state['df_ingredients_recipes'] = pd.read_csv(uploaded_file_ingr_recettes)
                
                # Chargement de l'historique des menus depuis Notion
                st.session_state['df_menus_hist'] = extract_menus()
                
                if (st.session_state['df_ingredients'] is not None and not st.session_state['df_ingredients'].empty and
                    st.session_state['df_planning'] is not None and not st.session_state['df_planning'].empty and
                    st.session_state['df_recipes'] is not None and not st.session_state['df_recipes'].empty and
                    st.session_state['df_ingredients_recipes'] is not None and not st.session_state['df_ingredients_recipes'].empty):
                    st.success("Toutes les données sont chargées avec succès.")
                    st.session_state['data_loaded'] = True
                else:
                    st.error("Veuillez charger tous les fichiers CSV et vérifier la connexion Notion.")
                    st.session_state['data_loaded'] = False
            except Exception as e:
                st.error(f"Une erreur est survenue lors du chargement : {e}")
                st.session_state['data_loaded'] = False

    if st.button("5. Réinitialiser les données"):
        st.session_state.clear()
        st.success("Toutes les variables de session ont été réinitialisées.")


st.header("Menu Généré")
if 'data_loaded' in st.session_state and st.session_state['data_loaded']:
    if st.button("6. Générer le menu"):
        with st.spinner("Génération du menu en cours..."):
            try:
                generator = MenuGenerator(
                    st.session_state['df_menus_hist'],
                    st.session_state['df_recipes'],
                    st.session_state['df_planning'],
                    st.session_state['df_ingredients'],
                    st.session_state['df_ingredients_recipes']
                )
                menu_genere = generator.generer_menu_planifie(st.session_state['df_recipes'], st.session_state['df_ingredients_recipes'])
                st.session_state['menu_genere'] = menu_genere
                st.success("Menu généré avec succès!")
            except ValueError as ve:
                st.error(f"Erreur de génération du menu : {ve}")
            except Exception as e:
                st.error(f"Une erreur inattendue est survenue lors de la génération du menu : {e}")

if 'menu_genere' in st.session_state and not st.session_state['menu_genere'].empty:
    st.dataframe(st.session_state['menu_genere'], use_container_width=True)
    
    csv_file = st.session_state['menu_genere'].to_csv(index=False).encode('utf-8')
    st.download_button(
        label="Télécharger le menu généré",
        data=csv_file,
        file_name='menu_genere.csv',
        mime='text/csv'
    )
else:
    st.info("Le menu généré s'affichera ici.")
