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

    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str): return 1
        if participants_str_codes == "B": return 1
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
                # Le log spécifique est déjà dans est_adaptee_aux_participants
                continue
            
            if self.est_recente(recette_id_str_cand, date_repas):
                # Le log spécifique est déjà dans est_recente
                continue

            if nutrition_req == "equilibré":
                try:
                    if self.recette_manager.df_recettes.index.name == COLONNE_ID_RECETTE:
                        calories = float(self.recette_manager.df_recettes.loc[recette_id_str_cand, "Calories"])
                    else:
                        calories = float(self.recette_manager.df_recettes[self.recette_manager.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_id_str_cand]["Calories"].iloc[0])
                    if calories > REPAS_EQUILIBRE:
                        logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) filtré: Calories ({calories}) > Équilibré ({REPAS_EQUILIBRE}).")
                        continue
                except (KeyError, ValueError, TypeError, IndexError):
                    logger.debug(f"Calories non valides/trouvées pour {nom_recette_cand} ({recette_id_str_cand}) (filtre nutrition).")
                    continue

            score_dispo, pourcentage_dispo, manquants_pour_cette_recette = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_cand, nb_personnes)
            recettes_scores_dispo[recette_id_str_cand] = score_dispo
            recettes_ingredients_manquants[recette_id_str_cand] = manquants_pour_cette_recette
            candidates.append(recette_id_str_cand)
            logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) ajouté: Score dispo {score_dispo:.2f}, {pourcentage_dispo:.0f}% d'ingrédients. Manquants: {len(manquants_pour_cette_recette)}")

            if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand):
                anti_gaspi_candidates.append(recette_id_str_cand)
                logger.debug(f"Candidat {nom_recette_cand} ({recette_id_str_cand}) est aussi anti-gaspi.")


        if not candidates:
            logger.debug("Aucun candidat trouvé après le filtrage initial.")
            return [], {}

        candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
        anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

        if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
            logger.debug(f"Priorisation des candidats anti-gaspi (meilleur score {recettes_scores_dispo.get(anti_gaspi_triees[0], -1):.2f}).")
            return anti_gaspi_triees[:5], recettes_ingredients_manquants
        
        logger.debug(f"Retourne les {min(len(candidates_triees), 10)} meilleurs candidats généraux.")
        return candidates_triees[:10], recettes_ingredients_manquants

    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str):
        logger.debug(f"--- Traitement Repas Standard pour {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_current_gen_set,
            transportable_req_str, temps_req_str, nutrition_req_str
        )
        if not recettes_candidates_initiales:
            logger.debug(f"Aucune recette candidate initiale pour {date_repas.strftime('%Y-%m-%d %H:%M')}.")
            return None, {}

        recettes_historiques_semaine_set = self.recettes_meme_semaine_annees_precedentes(date_repas)
        scores_candidats_dispo = {
            r_id: self.recette_manager.evaluer_disponibilite_et_manquants(r_id, participants_count_int)[0]
            for r_id in recettes_candidates_initiales
        }
        preferred_candidates_list = [r_id for r_id in recettes_candidates_initiales if r_id in recettes_historiques_semaine_set]
        if preferred_candidates_list:
            logger.debug(f"{len(preferred_candidates_list)} candidats préférés (historique semaine précédente) trouvés.")

        mots_cles_exclus_set = set()
        if menu_recent_noms_list:
            for nom_plat_recent in menu_recent_noms_list:
                if isinstance(nom_plat_recent, str) and nom_plat_recent.strip():
                    try: mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                    except IndexError: pass
        if mots_cles_exclus_set:
            logger.debug(f"Mots clés exclus pour anti-répétition (génération actuelle): {mots_cles_exclus_set}")

        def get_first_word_local(recette_id_str_func):
            nom = self.recette_manager.obtenir_nom(recette_id_str_func)
            return nom.lower().split()[0] if nom and nom.strip() and "Recette_ID_" not in nom else ""

        recette_choisie_final = None
        if preferred_candidates_list:
            preferred_valides_motcle = []
            for r_id in preferred_candidates_list:
                first_word = get_first_word_local(r_id)
                if first_word not in mots_cles_exclus_set:
                    preferred_valides_motcle.append(r_id)
                else:
                    logger.debug(f"Candidat préféré {self.recette_manager.obtenir_nom(r_id)} ({r_id}) filtré: Premier mot '{first_word}' déjà récent.")

            if preferred_valides_motcle:
                recette_choisie_final = sorted(preferred_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les préférées valides: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            else:
                recette_choisie_final = sorted(preferred_candidates_list, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les préférées (sans filtrage mot-clé, car tous sont filtrés): {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).") # Fallback, should ideally not happen if filtering is strict

        if not recette_choisie_final:
            candidates_valides_motcle = []
            for r_id in recettes_candidates_initiales:
                first_word = get_first_word_local(r_id)
                if first_word not in mots_cles_exclus_set:
                    candidates_valides_motcle.append(r_id)
                else:
                    logger.debug(f"Candidat général {self.recette_manager.obtenir_nom(r_id)} ({r_id}) filtré: Premier mot '{first_word}' déjà récent.")

            if candidates_valides_motcle:
                recette_choisie_final = sorted(candidates_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux valides: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            elif recettes_candidates_initiales:
                recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie parmi les candidats généraux (sans filtrage mot-clé, car tous sont filtrés): {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).") # Fallback

        if recette_choisie_final:
            logger.debug(f"Recette finale sélectionnée pour repas standard: {self.recette_manager.obtenir_nom(recette_choisie_final)} ({recette_choisie_final}).")
            return recette_choisie_final, recettes_manquants_dict.get(recette_choisie_final, {})
        logger.debug(f"Aucune recette finale sélectionnée pour repas standard à {date_repas.strftime('%Y-%m-%d %H:%M')}.")
        return None, {}

    def _log_decision_recette(self, recette_id_str, date_repas, participants_str_codes):
        if recette_id_str is not None:
            nom_recette = self.recette_manager.obtenir_nom(recette_id_str)
            adaptee = self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes) # This also logs
            temps_prep = self.recette_manager.obtenir_temps_preparation(recette_id_str)
            logger.debug(f"Décision rec {recette_id_str} ({nom_recette}): Adaptée={adaptee}, Temps={temps_prep} min")
        else:
            logger.warning(f"Aucune recette sélectionnée pour {date_repas.strftime('%d/%m/%Y')} - Participants: {participants_str_codes}")

    def _ajouter_resultat(self, resultats_liste, date_repas, nom_menu_str, participants_str_codes, remarques_str, temps_prep_int=0, recette_id_str_pour_eval=None):
        info_stock_str = ""
        if recette_id_str_pour_eval:
            score_dispo, pourcentage_dispo, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_pour_eval, self.compter_participants(participants_str_codes))
            info_stock_str = f"Stock: {pourcentage_dispo:.0f}% des ingrédients disponibles (score: {score_dispo:.2f})"

        remarques_finales = f"{remarques_str} {info_stock_str}".strip()
        resultats_liste.append({
            "Date": date_repas.strftime("%d/%m/%Y %H:%M"),
            COLONNE_NOM: nom_menu_str,
            "Participant(s)": participants_str_codes,
            "Remarques spécifiques": remarques_finales,
            "Temps de préparation": f"{temps_prep_int} min" if temps_prep_int else "-"
        })

    def generer_menu_repas_b(self, date_repas, plats_transportables_semaine_dict, repas_b_utilises_ids_list, menu_recent_noms_list):
        candidats_restes_ids = []
        # Tri par date, le plus ancien en premier, ce qui est logique pour les restes
        sorted_plats_transportables = sorted(plats_transportables_semaine_dict.items(), key=lambda item: item[0])

        logger.debug(f"--- Recherche de restes pour Repas B le {date_repas.strftime('%Y-%m-%d %H:%M')} ---")
        if not sorted_plats_transportables:
            logger.debug("Aucun plat transportable disponible dans plats_transportables_semaine_dict.")
            
        for date_plat_orig, plat_id_orig_str in sorted_plats_transportables:
            # Correction sécurité : s'assurer que date_plat_orig est bien un datetime
            if isinstance(date_plat_orig, str):
                date_plat_orig = pd.to_datetime(date_plat_orig, dayfirst=True)
            jours_ecoules = (date_repas.date() - date_plat_orig.date()).days
            
        for date_plat_orig, plat_id_orig_str in sorted_plats_transportables:
            nom_plat_reste = self.recette_manager.obtenir_nom(plat_id_orig_str)
            jours_ecoules = (date_repas.date() - date_plat_orig.date()).days
            
            logger.debug(f"Éval reste {nom_plat_reste} (ID: {plat_id_orig_str}) du {date_plat_orig.strftime('%Y-%m-%d')}. Jours écoulés: {jours_ecoules}.")

            if not (0 < jours_ecoules <= 2): # Condition: planifié dans les 2 jours précédents
                logger.debug(f"Reste {nom_plat_reste} filtré: Jours écoulés ({jours_ecoules}) hors de la plage (1-2 jours).")
                continue
            if plat_id_orig_str in repas_b_utilises_ids_list:
                logger.debug(f"Reste {nom_plat_reste} filtré: Déjà utilisé pour un repas B.")
                continue
            if not (nom_plat_reste and nom_plat_reste.strip() and "Recette_ID_" not in nom_plat_reste):
                logger.debug(f"Reste {nom_plat_reste} filtré: Nom de plat invalide ou générique.")
                continue
            
            # Vérification explicite que la recette d'origine est marquée comme transportable
            if not self.recette_manager.est_transportable(plat_id_orig_str): # Condition: transportable est 'oui'
                logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) filtré: La recette d'origine n'est pas marquée comme transportable dans Recettes.csv.")
                continue

            # ANCIENNE LOGIQUE D'ANTI-RÉPÉTITION, RETIRÉE POUR LES RESTES :
            # premier_mot_reste = nom_plat_reste.lower().split()[0]
            # mots_cles_recents_set = set()
            # if menu_recent_noms_list:
            #      for nom_plat_r in menu_recent_noms_list:
            #         if isinstance(nom_plat_r, str) and nom_plat_r.strip():
            #             try: mots_cles_recents_set.add(nom_plat_r.lower().split()[0])
            #             except IndexError: pass
            # if premier_mot_reste not in mots_cles_recents_set:
            #     candidats_restes_ids.append(plat_id_orig_str)
            #     logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) ajouté aux candidats restes.")
            
            # Nouvelle logique : Tous les restes valides sont ajoutés si les conditions précédentes sont respectées.
            candidats_restes_ids.append(plat_id_orig_str)
            logger.debug(f"Reste {nom_plat_reste} (ID: {plat_id_orig_str}) ajouté aux candidats restes (pas de filtrage anti-répétition pour les restes).")


        if candidats_restes_ids:
            plat_id_choisi_str = candidats_restes_ids[0] # Choisit le reste transportable le plus ancien et valide
            nom_plat_choisi_str = self.recette_manager.obtenir_nom(plat_id_choisi_str)
            repas_b_utilises_ids_list.append(plat_id_choisi_str)
            logger.info(f"Reste choisi pour Repas B: {nom_plat_choisi_str} (ID: {plat_id_choisi_str}).")
            return f"Restes : {nom_plat_choisi_str}", plat_id_choisi_str, "Reste transportable utilisé"

        logger.info("Pas de reste disponible trouvé pour ce Repas B.")
        return "Pas de reste disponible", None, "Aucun reste transportable trouvé"


    def generer_menu(self):
        resultats_df_list = []
        repas_b_utilises_ids = []
        plats_transportables_semaine = {} # Réinitialisé à chaque génération
        used_recipes_current_generation_set = set() # Pour éviter les doublons dans la même génération
        menu_recent_noms = [] # Pour la logique d'anti-répétition des premiers mots
        ingredients_effectivement_utilises_ids_set = set() # Non utilisé pour la liste de courses finale, mais pour le suivi
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
                # Only consider meals that were *explicitly marked* as transportable in the planning
                # and subsequently chosen for a standard meal.
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, 1, date_repas_dt)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    # REMOVED: This logic was incorrect. A Repas B is already a leftover and shouldn't be added back
                    # to the pool of 'original transportable meals'.
                    # if date_repas_dt.weekday() >= 5:
                    #      plats_transportables_semaine[date_repas_dt] = recette_choisie_id
            else: # Repas standard
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
                
                # Only add to plats_transportables_semaine if the *planning itself* requested a transportable meal
                # AND the chosen recipe is indeed transportable.
                # This applies only to standard meals, not Repas B.
                if participants_str != "B" and self.recette_manager.est_transportable(recette_choisie_id):
                    plats_transportables_semaine[date_repas_dt] = recette_choisie_id

                    logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) ajouté à plats_transportables_semaine pour le {date_repas_dt.strftime('%Y-%m-%d')}.")
                elif participants_str != "B": # Log why it's not added if not a Repas B
                    logger.debug(f"'{nom_plat_final}' ({recette_choisie_id}) non ajouté à plats_transportables_semaine (transportable_req est '{transportable_req}' ou recette non transportable).")


                for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                    current_qte = self.ingredients_a_acheter_cumules.get(ing_id, 0.0)
                    self.ingredients_a_acheter_cumules[ing_id] = current_qte + qte_manquante
                    logger.debug(f"Ingrédient manquant cumulé: {self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)} - {qte_manquante:.2f} (total: {self.ingredients_a_acheter_cumules[ing_id]:.2f})")

            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)

            self._ajouter_resultat(
                resultats_df_list, date_repas_dt, nom_plat_final, participants_str,
                remarques_repas, temps_prep_final, recette_choisie_id
            )
            # Gestion de menu_recent_noms pour l'anti-répétition des premiers mots
            if nom_plat_final and "Pas de recette" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Erreur" not in nom_plat_final and "Invalide" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                if len(menu_recent_noms) > 3: # Garder les 3 derniers noms de plats pour la logique anti-répétition
                    menu_recent_noms.pop(0)


        df_menu_genere = pd.DataFrame(resultats_df_list)

        liste_courses_final = {}
        for ing_id, qte_cumulee in self.ingredients_a_acheter_cumules.items():
            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            if nom_ing and "ID_Ing_" not in nom_ing:
                # Essayer de récupérer l'unité de l'ingrédient
                unite_ing = "unité(s)"
                try:
                    unite_ing_df = self.recette_manager.df_ingredients_initial[
                        self.recette_manager.df_ingredients_initial[COLONNE_ID_INGREDIENT].astype(str) == ing_id
                    ]
                    if not unite_ing_df.empty and 'unité' in unite_ing_df.columns:
                        unite_ing = unite_ing_df['unité'].iloc[0]
                except Exception as e:
                    logger.warning(f"Impossible de récupérer l'unité pour l'ingrédient {nom_ing}: {e}")

                liste_courses_final[nom_ing] = f"{qte_cumulee:.2f} {unite_ing}"
            else:
                liste_courses_final[f"ID Ingrédient {ing_id}"] = f"{qte_cumulee:.2f} unité(s) (Nom non trouvé)"


        if not df_menu_genere.empty:
            logger.info(f"Nombre de lignes totales générées : {len(df_menu_genere)}")
            if 'Date' in df_menu_genere.columns:
                df_menu_genere['Date'] = pd.to_datetime(df_menu_genere['Date'], format="%d/%m/%Y %H:%M", errors='coerce').dt.strftime('%Y-%m-%d %H:%M')

        # Convertir la liste de courses en un format plus simple pour le retour
        formatted_liste_courses = []
        for ing, qte_unite in liste_courses_final.items():
            formatted_liste_courses.append(f"{ing}: {qte_unite}")
        formatted_liste_courses.sort() # Tri alphabétique

        return df_menu_genere, formatted_liste_courses


# --- Streamlit UI ---

def main():
    st.set_page_config(layout="wide", page_title="Générateur de Menus et Liste de Courses")
    st.title("🍽️ Générateur de Menus et Liste de Courses")
    st.markdown("---")

    st.sidebar.header("Chargement des fichiers CSV")
    st.sidebar.info("Veuillez charger tous les fichiers CSV nécessaires.")

    # Individual uploaders for the constant files
    uploaded_files = {}
    
    file_names_individual = ["Recettes.csv", "Ingredients.csv", "Ingredients_recettes.csv"]
    for file_name in file_names_individual:
        uploaded_files[file_name] = st.sidebar.file_uploader(f"Uploader {file_name}", type="csv", key=file_name)

    # Combined uploader for Planning.csv and Menus.csv
    uploaded_planning_menus = st.sidebar.file_uploader(
        "Uploader Planning.csv et Menus.csv (sélectionnez les deux)",
        type="csv",
        accept_multiple_files=True,
        key="planning_menus_uploader"
    )

    dataframes = {}
    all_files_uploaded = True

    # Process individual uploads first
    for file_name, uploaded_file in uploaded_files.items():
        if uploaded_file is not None:
            try:
                df = pd.read_csv(uploaded_file, encoding='utf-8')
                # ... (rest of your existing type conversions for Recettes, Ingredients, Ingredients_recettes)
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
            # Don't break here, allow the combined uploader to be checked
            # if this file is not essential for initial check
            pass # Keep looping to check other individual files

    # Process combined Planning and Menus uploads
    if uploaded_planning_menus:
        found_planning = False
        found_menus = False
        for uploaded_file in uploaded_planning_menus:
            file_name = uploaded_file.name
            try:
                if "Planning.csv" in file_name:
                    df = pd.read_csv(
                        uploaded_file,
                        encoding='utf-8',
                        sep=';',
                        parse_dates=['Date'],
                        dayfirst=True
                    )
                    dataframes["Planning"] = df
                    st.sidebar.success("Planning.csv chargé avec succès.")
                    found_planning = True
                elif "Menus.csv" in file_name:
                    df = pd.read_csv(uploaded_file, encoding='utf-8')
                    dataframes["Menus"] = df
                    st.sidebar.success("Menus.csv chargé avec succès.")
                    found_menus = True
            except Exception as e:
                st.sidebar.error(f"Erreur lors du chargement de {file_name}: {e}")
                all_files_uploaded = False # Indicate failure if any of these fail
        
        if not found_planning:
            st.sidebar.warning("Planning.csv n'a pas été trouvé parmi les fichiers sélectionnés.")
            all_files_uploaded = False
        if not found_menus:
            st.sidebar.warning("Menus.csv n'a pas été trouvé parmi les fichiers sélectionnés.")
            all_files_uploaded = False
    else:
        st.sidebar.warning("Veuillez uploader Planning.csv et Menus.csv.")
        all_files_uploaded = False


    # Final check if all required dataframes are present
    required_dfs = ["Recettes", "Planning", "Menus", "Ingredients", "Ingredients_recettes"]
    for df_name in required_dfs:
        if df_name not in dataframes:
            all_files_uploaded = False
            break

    if not all_files_uploaded:
        st.warning("Veuillez charger tous les fichiers CSV nécessaires pour continuer.")
        return

    # Vérification des colonnes essentielles après le chargement
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
                # Initialisation de MenuGenerator avec les DataFrames chargés
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

                # Suppose que df_menu_genere est ton DataFrame de menu après génération

                # Ajuste l'ordre et les noms des colonnes pour correspondre exactement à l’exemple CSV
                df_export = df_menu_genere.copy()
                
                # Renomme ou crée les colonnes "Nom" et "Participant(s)" si nécessaire selon ton DF actuel
                # Ici on s’assure d’avoir la bonne casse et noms
                df_export = df_export.rename(columns={
                    'Participant(s)': 'Participant(s)',  # adapte si tu as un nom différent
                    COLONNE_NOM: 'Nom',
                    'Date': 'Date'
                })
                
                # Si besoin, convertit la colonne Date au format "yyyy-mm-dd HH:MM"
                if not pd.api.types.is_datetime64_any_dtype(df_export['Date']):
                    df_export['Date'] = pd.to_datetime(df_export['Date'], errors='coerce')
                df_export['Date'] = df_export['Date'].dt.strftime('%Y-%m-%d %H:%M')
                
                # Filtrer les colonnes pour n’avoir que celles-ci, dans cet ordre
                df_export = df_export[['Date', 'Participant(s)', 'Nom']]
                
                # Génére la chaîne CSV avec séparateur virgule, BOM UTF-8 (si nécessaire)
                csv_data = df_export.to_csv(index=False, sep=',', encoding='utf-8-sig')
                
                # Bouton de téléchargement Streamlit (à placer dans ta plage de code UI)
                st.download_button(
                    label="📥 Télécharger le menu en CSV",
                    data=csv_data,
                    file_name="menu_genere.csv",
                    mime="text/csv"
                )


                st.header("3. Liste de Courses (Ingrédients manquants cumulés)")
                if liste_courses:
                    # Convertir la liste de courses formatée en un DataFrame pour l'affichage et l'export
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
