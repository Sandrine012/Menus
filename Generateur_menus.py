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
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        st.error(f"Colonnes manquantes dans {nom_fichier}: {', '.join(colonnes_manquantes)}")
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
            # Force conversion to numeric, coercing errors to NaN, then fill NaN with 0
            self.stock_simule["Qte reste"] = pd.to_numeric(
                self.stock_simule["Qte reste"].astype(str).str.replace(',', '.'), errors='coerce'
            ).fillna(0).astype(float)
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
            logger.error(f"Erreur globale calcul_quantite_necessaire pour {recette_id_str}: {e}")
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

            logger.debug(f"Ingr {ing_id_str} rec {recette_id_str}: stock={qte_en_stock}, nec={qte_necessaire}, ratio={ratio_dispo:.2f}, manquant={ingredients_manquants.get(ing_id_str, 0):.2f}")

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
            
            # If any active participant is in the "n'aime pas" list for this recipe, it's not adapted.
            is_adapted = not any(code_participant in n_aime_pas for code_participant in participants_actifs)
            if not is_adapted:
                logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} (ID: {recette_page_id_str}) rejetée: N'aime pas ({n_aime_pas}) vs Participants ({participants_actifs}).")
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
            return valeur == "oui"
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour transportable (valeur par défaut Faux).")
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
            logger.debug(f"Temps de prép manquant pour recette {recette_page_id_str}. Valeur par défaut {VALEUR_DEFAUT_TEMPS_PREPARATION} min.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (KeyError, IndexError):
            logger.debug(f"Recette ID {recette_page_id_str} non trouvée pour temps_preparation.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except (ValueError, TypeError):
            logger.warning(f"Temps de prép non valide pour recette {recette_page_id_str}. Valeur par défaut {VALEUR_DEFAUT_TEMPS_PREPARATION} min.")
            return VALEUR_DEFAUT_TEMPS_PREPARATION
        except Exception as e:
            logger.error(f"Erreur obtention temps prép pour {recette_page_id_str}: {e}")
            return VALEUR_DEFAUT_TEMPS_PREPARATION

class MenusHistoryManager:
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()
        self.df_menus_historique["Date"] = pd.to_datetime(
            self.df_menus_historique["Date"],
            errors="coerce"
        )
        self.df_menus_historique.dropna(subset=["Date"], inplace=True)
        # Ajout de la colonne 'Semaine' si elle n'existe pas, calculée à partir de la 'Date'
        if "Semaine" not in self.df_menus_historique.columns:
            self.df_menus_historique["Semaine"] = self.df_menus_historique["Date"].dt.isocalendar().week.astype(int)
            logger.info("Colonne 'Semaine' générée pour l'historique des menus.")

class MenuGenerator:
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
            # La colonne 'Semaine' est maintenant garantie d'exister par MenusHistoryManager
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Semaine', 'Recette']):
                logger.debug("Historique des menus vide ou colonnes manquantes pour recettes_meme_semaine_annees_precedentes.")
                return set()

            semaine_actuelle = date_actuelle.isocalendar()[1]
            annee_actuelle = date_actuelle.year

            df_menus_semaine = df_hist[
                (df_hist["Semaine"].astype(int) == semaine_actuelle) &
                (df_hist["Date"].dt.year < annee_actuelle) &
                pd.notna(df_hist["Recette"])
            ]
            
            recipes_this_week_past_years = set(df_menus_semaine["Recette"].astype(str).unique())
            logger.debug(f"Recettes semaine {semaine_actuelle} des années précédentes: {recipes_this_week_past_years}")
            return recipes_this_week_past_years
        except Exception as e:
            logger.error(f"Erreur recettes_meme_semaine_annees_precedentes pour {date_actuelle}: {e}")
            return set()

    def est_recente(self, recette_page_id_str, date_actuelle):
        try:
            df_hist = self.menus_history_manager.df_menus_historique
            if df_hist.empty or not all(col in df_hist.columns for col in ['Date', 'Recette']):
                return False

            debut = date_actuelle - timedelta(days=NB_JOURS_ANTI_REPETITION)
            fin   = date_actuelle + timedelta(days=NB_JOURS_ANTI_REPETITION)
            mask  = (
                (df_hist['Recette'].astype(str) == str(recette_page_id_str)) &
                (df_hist['Date'] >= debut) &
                (df_hist['Date'] <= fin)
            )
            is_recent = not df_hist.loc[mask].empty
            if is_recent:
                logger.debug(f"Recette {self.recette_manager.obtenir_nom(recette_page_id_str)} (ID: {recette_page_id_str}) est récente.")
            return is_recent
        except Exception as e:
            logger.error(f"Erreur est_recente pour {recette_page_id_str} à {date_actuelle}: {e}")
            return False

    def compter_participants(self, participants_str_codes):
        if not isinstance(participants_str_codes, str): return 1
        if participants_str_codes == "B": return 1 # 'B' is always 1 person for this logic
        return len([p for p in participants_str_codes.replace(" ", "").split(",") if p])

    def _filtrer_recette_base(self, recette_id_str, participants_str_codes):
        return self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)

    def generer_recettes_candidates(self, date_repas, participants_str_codes, used_recipes_in_current_gen, transportable_req, temps_req, nutrition_req, apply_anti_repetition=True):
        candidates = []
        anti_gaspi_candidates = []
        recettes_scores_dispo = {}
        recettes_ingredients_manquants = {}

        nb_personnes = self.compter_participants(participants_str_codes)
        
        all_recettes_ids = self.recette_manager.df_recettes.index.astype(str).tolist()
        logger.debug(f"Total de {len(all_recettes_ids)} recettes pour la génération des candidats.")

        for recette_id_str_cand in all_recettes_ids:
            recette_nom = self.recette_manager.obtenir_nom(recette_id_str_cand)

            if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
                logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Non transportable selon la requête.")
                continue

            temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
            if temps_req == "express" and temps_total > TEMPS_MAX_EXPRESS:
                logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Temps ({temps_total} min) > Express ({TEMPS_MAX_EXPRESS} min).")
                continue
            if temps_req == "rapide" and temps_total > TEMPS_MAX_RAPIDE:
                logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Temps ({temps_total} min) > Rapide ({TEMPS_MAX_RAPIDE} min).")
                continue

            if recette_id_str_cand in used_recipes_in_current_gen:
                logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Déjà utilisée dans la génération actuelle.")
                continue
            
            if not self._filtrer_recette_base(recette_id_str_cand, participants_str_codes):
                # _filtrer_recette_base logs rejection reason internally
                continue
            
            # Application de l'anti-répétition conditionnelle
            if apply_anti_repetition and self.est_recente(recette_id_str_cand, date_repas):
                logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Récente selon l'anti-répétition stricte.")
                continue

            if nutrition_req == "equilibré":
                try:
                    if self.recette_manager.df_recettes.index.name == COLONNE_ID_RECETTE:
                        calories = self.recette_manager.df_recettes.loc[recette_id_str_cand, "Calories"]
                    else:
                        calories = self.recette_manager.df_recettes[self.recette_manager.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_id_str_cand]["Calories"].iloc[0]
                    
                    if pd.isna(calories):
                        logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) ignorée pour nutrition: Calories manquantes.")
                        continue
                    
                    calories = float(calories) # Ensure numeric conversion
                    if calories > REPAS_EQUILIBRE:
                        logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) rejetée: Calories ({calories:.0f}) > Équilibré ({REPAS_EQUILIBRE}).")
                        continue
                except (KeyError, ValueError, TypeError, IndexError):
                    logger.debug(f"Recette {recette_nom} (ID: {recette_id_str_cand}) ignorée pour nutrition: Calories non valides/trouvées.")
                    continue

            score_dispo, _, manquants_pour_cette_recette = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_cand, nb_personnes)
            recettes_scores_dispo[recette_id_str_cand] = score_dispo
            recettes_ingredients_manquants[recette_id_str_cand] = manquants_pour_cette_recette
            candidates.append(recette_id_str_cand)

            if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand):
                anti_gaspi_candidates.append(recette_id_str_cand)

        if not candidates:
            logger.info(f"Aucun candidat trouvé pour la date {date_repas.strftime('%d/%m/%Y %H:%M')} avec les critères actuels.")
            return [], {}

        candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
        anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

        if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
            logger.debug(f"Retour de {len(anti_gaspi_triees[:5])} candidats anti-gaspi.")
            return anti_gaspi_triees[:5], recettes_ingredients_manquants
        
        logger.debug(f"Retour de {len(candidates_triees[:10])} candidats généraux.")
        return candidates_triees[:10], recettes_ingredients_manquants

    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str):
        # Première tentative avec toutes les règles (incluant l'anti-répétition stricte)
        logger.info(f"Tentative de génération STANDARD pour {date_repas.strftime('%d/%m/%Y %H:%M')} avec anti-répétition stricte.")
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_current_gen_set,
            transportable_req_str, temps_req_str, nutrition_req_str, apply_anti_repetition=True
        )

        recette_choisie_final = None
        remarques_repas = "Plat généré"

        def get_first_word_local(recette_id_str_func):
            nom = self.recette_manager.obtenir_nom(recette_id_str_func)
            return nom.lower().split()[0] if nom and nom.strip() and "Recette_ID_" not in nom else ""

        if recettes_candidates_initiales:
            recettes_historiques_semaine_set = self.recettes_meme_semaine_annees_precedentes(date_repas)
            scores_candidats_dispo = {
                r_id: self.recette_manager.evaluer_disponibilite_et_manquants(r_id, participants_count_int)[0]
                for r_id in recettes_candidates_initiales
            }
            
            # Prioriser les recettes historiques de la même semaine
            preferred_candidates_list = [r_id for r_id in recettes_candidates_initiales if r_id in recettes_historiques_semaine_set]

            mots_cles_exclus_set = set()
            if menu_recent_noms_list:
                for nom_plat_recent in menu_recent_noms_list:
                    if isinstance(nom_plat_recent, str) and nom_plat_recent.strip():
                        try: mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                        except IndexError: pass
            
            # Filter candidates based on excluded first words
            candidates_filtered_by_first_word = [
                r_id for r_id in (preferred_candidates_list if preferred_candidates_list else recettes_candidates_initiales)
                if get_first_word_local(r_id) not in mots_cles_exclus_set
            ]

            if candidates_filtered_by_first_word:
                recette_choisie_final = sorted(candidates_filtered_by_first_word, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie (strict, 1st word filter): {self.recette_manager.obtenir_nom(recette_choisie_final)}")
            elif preferred_candidates_list: # If no candidate after first word filter, but preferred candidates existed, take the best of them
                 recette_choisie_final = sorted(preferred_candidates_list, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                 logger.debug(f"Recette choisie (strict, 1st word filtered, fallback to preferred): {self.recette_manager.obtenir_nom(recette_choisie_final)}")
            elif recettes_candidates_initiales: # If no preferred, no first word filtered, take the best from all initial candidates
                recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
                logger.debug(f"Recette choisie (strict, fallback to any initial): {self.recette_manager.obtenir_nom(recette_choisie_final)}")


        # Mécanisme de repli : Si aucune recette n'a été trouvée avec les règles strictes
        if not recette_choisie_final:
            logger.info(f"Aucune recette trouvée avec les contraintes strictes pour {date_repas.strftime('%d/%m/%Y %H:%M')}. Tentative sans anti-répétition stricte (NB_JOURS_ANTI_REPETITION) et sans filtre sur le premier mot.")
            recettes_candidates_relaxed, recettes_manquants_dict_relaxed = self.generer_recettes_candidates(
                date_repas, participants_str_codes, used_recipes_current_gen_set,
                transportable_req_str, temps_req_str, nutrition_req_str, apply_anti_repetition=False # Deuxième passage : SANS anti-répétition sur l'historique récent
            )

            if recettes_candidates_relaxed:
                scores_candidats_dispo_relaxed = {
                    r_id: self.recette_manager.evaluer_disponibilite_et_manquants(r_id, participants_count_int)[0]
                    for r_id in recettes_candidates_relaxed
                }
                # Prioriser les recettes historiques de la même semaine si possible, même en mode relaxé
                preferred_candidates_relaxed = [r_id for r_id in recettes_candidates_relaxed if r_id in self.recettes_meme_semaine_annees_precedentes(date_repas)]

                if preferred_candidates_relaxed:
                    recette_choisie_final = sorted(preferred_candidates_relaxed, key=lambda r_id: scores_candidats_dispo_relaxed.get(r_id, -1), reverse=True)[0]
                    logger.debug(f"Recette choisie (relaxé, preferred): {self.recette_manager.obtenir_nom(recette_choisie_final)}")
                else: # Sinon, prendre la meilleure tout court
                    recette_choisie_final = sorted(recettes_candidates_relaxed, key=lambda r_id: scores_candidats_dispo_relaxed.get(r_id, -1), reverse=True)[0]
                    logger.debug(f"Recette choisie (relaxé, any): {self.recette_manager.obtenir_nom(recette_choisie_final)}")

                recettes_manquants_dict = recettes_manquants_dict_relaxed
                remarques_repas = "Plat généré (règles d'anti-répétition assouplies)"
                logger.info(f"Recette trouvée après assouplissement: {self.recette_manager.obtenir_nom(recette_choisie_final)}")
            else:
                remarques_repas = "Aucune recette n'a pu être sélectionnée, même après assouplissement."
                logger.warning(f"Aucune recette trouvée du tout pour {date_repas.strftime('%d/%m/%Y %H:%M')}")

        return recette_choisie_final, recettes_manquants_dict.get(recette_choisie_final, {}), remarques_repas

    def generer_menu_repas_b(self, date_repas, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms_list):
        """
        Génère un repas pour le participant 'B', priorisant les restes transportables
        de la semaine, puis une nouvelle recette transportable rapide.
        """
        logger.info(f"Tentative de génération REPAS B pour {date_repas.strftime('%d/%m/%Y %H:%M')}")

        # 1. Prioriser les restes transportables de la semaine
        restes_potentials = []
        current_week_num = date_repas.isocalendar().week

        # Filter plats_transportables_semaine to include only those from the current week
        # and not yet used by participant 'B'
        for dt_plat, recette_id in list(plats_transportables_semaine.items()): # Iterate on a copy as items might be deleted
            if dt_plat.isocalendar().week == current_week_num:
                if recette_id not in repas_b_utilises_ids:
                    # Also check if the recipe still exists in our main recipe df
                    if str(recette_id) in self.recette_manager.df_recettes.index.astype(str):
                         restes_potentials.append(recette_id)
                    else:
                        logger.warning(f"Plat transportable {recette_id} pour le reste introuvable dans la liste des recettes. Supprimé des potentiels.")
                        del plats_transportables_semaine[dt_plat] # Clean up if recipe no longer exists

        if restes_potentials:
            recette_id = random.choice(restes_potentials)
            repas_b_utilises_ids.append(recette_id) # Mark as used for B
            nom_plat = self.recette_manager.obtenir_nom(recette_id)
            logger.info(f"Repas B: Reste choisi: {nom_plat} (ID: {recette_id})")
            return f"Reste: {nom_plat}", recette_id, f"Reste du plat : {nom_plat}"

        # 2. Si pas de restes, chercher une nouvelle recette transportable rapide
        candidates_transportables = []
        
        def get_first_word_local(recette_id_str_func):
            nom = self.recette_manager.obtenir_nom(recette_id_str_func)
            return nom.lower().split()[0] if nom and nom.strip() and "Recette_ID_" not in nom else ""

        mots_cles_exclus_set = set()
        if menu_recent_noms_list:
            for nom_plat_recent in menu_recent_noms_list:
                if isinstance(nom_plat_recent, str) and nom_plat_recent.strip():
                    try: mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                    except IndexError: pass

        for recette_id_cand in self.recette_manager.df_recettes.index.astype(str):
            recette_nom_cand = self.recette_manager.obtenir_nom(recette_id_cand)

            if not self.recette_manager.est_transportable(recette_id_cand):
                logger.debug(f"Repas B cand {recette_nom_cand} (ID: {recette_id_cand}) rejetée: Non transportable.")
                continue
            
            temps_prep = self.recette_manager.obtenir_temps_preparation(recette_id_cand)
            if temps_prep > TEMPS_MAX_RAPIDE:
                logger.debug(f"Repas B cand {recette_nom_cand} (ID: {recette_id_cand}) rejetée: Temps ({temps_prep} min) > Rapide ({TEMPS_MAX_RAPIDE} min).")
                continue
            
            if self.est_recente(recette_id_cand, date_repas):
                logger.debug(f"Repas B cand {recette_nom_cand} (ID: {recette_id_cand}) rejetée: Récente (anti-répétition stricte).")
                continue
            
            if not self._filtrer_recette_base(recette_id_cand, "B"): # 'B' is just one person, assume always adapted to them unless specified in Aime_pas_princip
                logger.debug(f"Repas B cand {recette_nom_cand} (ID: {recette_id_cand}) rejetée: Non adaptée au participant B.")
                continue

            # Anti-répétition sur le premier mot par rapport aux menus récents
            first_word_cand = get_first_word_local(recette_id_cand)
            if first_word_cand and first_word_cand in mots_cles_exclus_set:
                logger.debug(f"Repas B cand {recette_nom_cand} (ID: {recette_id_cand}) rejetée: Mot-clé récent déjà utilisé ('{first_word_cand}').")
                continue

            candidates_transportables.append(recette_id_cand)

        if candidates_transportables:
            recette_id = random.choice(candidates_transportables)
            nom_plat = self.recette_manager.obtenir_nom(recette_id)
            logger.info(f"Repas B: Nouvelle recette transportable: {nom_plat} (ID: {recette_id})")
            return nom_plat, recette_id, "Plat transportable généré"

        logger.warning(f"Repas B: Pas de reste disponible et aucune nouvelle recette transportable trouvée pour {date_repas.strftime('%d/%m/%Y %H:%M')}.")
        return "Pas de reste ou plat transportable trouvé", None, "Aucun reste ou plat transportable disponible"

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
    
    def _log_decision_recette(self, recette_id, date_repas, participants):
        """Log the decision made for a specific recipe."""
        if recette_id:
            nom_recette = self.recette_manager.obtenir_nom(recette_id)
            logger.info(f"DECISION pour {date_repas.strftime('%d/%m/%Y %H:%M')} ({participants}): Recette choisie: {nom_recette} (ID: {recette_id})")
        else:
            logger.info(f"DECISION pour {date_repas.strftime('%d/%m/%Y %H:%M')} ({participants}): Aucune recette trouvée.")

    def generer_menu(self):
        resultats_df_list = []
        repas_b_utilises_ids = [] # To track which recipes have been served as 'B' leftovers in the current generation
        plats_transportables_semaine = {} # Key: datetime, Value: recipe_id. Store transportable meals cooked on weekends.
        used_recipes_current_generation_set = set() # To prevent strict recipe repetition within the current planned period for non-B meals.
        menu_recent_noms = [] # List of recent recipe names (first word) for additional anti-repetition.
        self.ingredients_a_acheter_cumules = {} # Réinitialiser la liste de courses

        for _, repas_planning_row in self.df_planning.sort_values("Date").iterrows():
            date_repas_dt = repas_planning_row["Date"]
            participants_str = str(repas_planning_row["Participants"])
            participants_count = self.compter_participants(participants_str)
            transportable_req = str(repas_planning_row.get("Transportable", "")).strip().lower()
            temps_req = str(repas_planning_row.get("Temps", "")).strip().lower()
            nutrition_req = str(repas_planning_row.get("Nutrition", "")).strip().lower()

            logger.info(f"--- Traitement {date_repas_dt.strftime('%d/%m/%Y %H:%M')} - Participants: {participants_str} ---")

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = ""
            temps_prep_final = 0
            ingredients_manquants_pour_recette_choisie = {}

            # Clear plats_transportables_semaine at the start of a new week
            # Find the start of the current week (Monday)
            current_week_start = date_repas_dt - timedelta(days=date_repas_dt.weekday())
            plats_transportables_semaine_filtered = {}
            for dt, r_id in plats_transportables_semaine.items():
                if dt >= current_week_start:
                    plats_transportables_semaine_filtered[dt] = r_id
            plats_transportables_semaine = plats_transportables_semaine_filtered
            
            # Clear repas_b_utilises_ids for a new week
            # Assuming repas_b_utilises_ids is only for the *current* week's leftovers
            if date_repas_dt.weekday() == 0 and repas_b_utilises_ids: # If it's Monday and there were used B recipes
                logger.info("Réinitialisation de repas_b_utilises_ids pour la nouvelle semaine.")
                repas_b_utilises_ids = []


            if participants_str == "B":
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    # Note: For 'B' meals, we're not explicitly calculating ingredients_manquants_pour_recette_choisie here
                    # because the primary goal is to use what's available (leftovers) or generate a simple new meal.
                    # Stock decrement is handled, but the missing list might not be crucial for 'B' as much as for main meals.
                    self.recette_manager.decrementer_stock(recette_choisie_id, 1, date_repas_dt) # For 1 person
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
            else:
                recette_choisie_id, ingredients_manquants_pour_recette_choisie, remarques_repas = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count,
                    used_recipes_current_generation_set, menu_recent_noms,
                    transportable_req, temps_req, nutrition_req
                )

                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    used_recipes_current_generation_set.add(recette_choisie_id)
                    self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    
                    # Store transportable meals made on weekends for potential 'B' use
                    if date_repas_dt.weekday() >= 5 and self.recette_manager.est_transportable(recette_choisie_id): # Saturday or Sunday
                        plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                        logger.debug(f"Plat '{nom_plat_final}' enregistré comme transportable pour la semaine.")

                    # Mise à jour de la liste de courses cumulée
                    for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                        nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
                        if nom_ing:
                            # Use nom_ing as key for cumulation, as we want to sum quantities for the same ingredient
                            self.ingredients_a_acheter_cumules[nom_ing] = self.ingredients_a_acheter_cumules.get(nom_ing, 0.0) + qte_manquante
                            logger.debug(f"Ajout à la liste de courses: {nom_ing} ({qte_manquante:.2f}) pour recette {nom_plat_final}")
                else:
                    nom_plat_final = "Pas de recette trouvée"
                    # remarques_repas is already defined by _traiter_menu_standard in this case.

            # Mettre à jour les noms des plats récents pour l'anti-répétition des premiers mots
            if nom_plat_final and "Erreur" not in nom_plat_final and "Pas de reste" not in nom_plat_final and "Pas de recette trouvée" not in nom_plat_final:
                # Keep track of the last 3 distinct first words (or whole names if short)
                current_first_word = nom_plat_final.lower().split()[0] if nom_plat_final.strip() else ""
                
                # Prevent adding duplicates to recent names
                if len(menu_recent_noms) >= 3:
                    menu_recent_noms.pop(0) # Remove oldest
                menu_recent_noms.append(nom_plat_final) # Add current full name
            
            logger.debug(f"Menu récents après traitement: {menu_recent_noms}")

            self._ajouter_resultat(resultats_df_list, date_repas_dt, nom_plat_final, participants_str, remarques_repas, temps_prep_final, recette_choisie_id)
            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)


        df_menu_genere = pd.DataFrame(resultats_df_list)
        return df_menu_genere, self.ingredients_a_acheter_cumules # Return the menu DataFrame and the shopping list

def main():
    st.set_page_config(page_title="Générateur de Menus", layout="wide")
    st.title("🍽️ Générateur de Menus Automatisé")
    st.markdown("""
        Chargez vos fichiers CSV pour générer un planning de repas et une liste de courses.
        Assurez-vous que les fichiers CSV sont encodés en UTF-8. Les fichiers 'Recettes.csv', 'Ingredients.csv', 'Ingredients_recettes.csv' et 'Menus.csv' doivent utiliser la virgule (`,`) comme délimiteur, tandis que le fichier 'Planning.csv' doit utiliser le point-virgule (`;`).
    """)

    st.header("1. Chargement des fichiers de données")

    # Uploaders de fichiers
    uploaded_file_recettes = st.file_uploader("Charger Recettes.csv", type="csv")
    uploaded_file_planning = st.file_uploader("Charger Planning.csv", type="csv")
    uploaded_file_ingredients = st.file_uploader("Charger Ingredients.csv", type="csv")
    uploaded_file_ingredients_recettes = st.file_uploader("Charger Ingredients_recettes.csv", type="csv")
    uploaded_file_menus_hist = st.file_uploader("Charger Menus.csv (historique)", type="csv")

    dataframes = {}
    files_to_load = {
        "Recettes": uploaded_file_recettes,
        "Planning": uploaded_file_planning,
        "Ingredients": uploaded_file_ingredients,
        "Ingredients_recettes": uploaded_file_ingredients_recettes,
        "Menus": uploaded_file_menus_hist,
    }

    all_files_uploaded = True
    for name, uploaded_file in files_to_load.items():
        if uploaded_file is not None:
            try:
                # Logique pour déterminer le séparateur : virgule pour Recettes, Ingredients, Ingredients_recettes et Menus, point-virgule pour Planning
                separator = ',' if name in ["Recettes", "Ingredients", "Ingredients_recettes", "Menus"] else ';'
                # Tente de lire avec utf-8, puis latin1 si utf-8 échoue
                df = pd.read_csv(uploaded_file, sep=separator, encoding='utf-8')
                dataframes[name] = df
                st.success(f"'{name}.csv' chargé avec succès.")
            except UnicodeDecodeError:
                try:
                    uploaded_file.seek(0) # Revenir au début du fichier
                    separator = ',' if name in ["Recettes", "Ingredients", "Ingredients_recettes", "Menus"] else ';'
                    df = pd.read_csv(uploaded_file, sep=separator, encoding='latin1')
                    dataframes[name] = df
                    st.warning(f"'{name}.csv' chargé avec succès en utilisant l'encodage 'latin1'.")
                except Exception as e:
                    st.error(f"Erreur de lecture de '{name}.csv': {e}. Assurez-vous que le fichier est un CSV valide et utilise le bon délimiteur (virgule pour Recettes.csv, Ingredients.csv, Ingredients_recettes.csv et Menus.csv, point-virgule pour Planning.csv).")
                    all_files_uploaded = False
            except Exception as e:
                st.error(f"Erreur de lecture de '{name}.csv': {e}. Assurez-vous que le fichier est un CSV valide et utilise le bon délimiteur (virgule pour Recettes.csv, Ingredients.csv, Ingredients_recettes.csv et Menus.csv, point-virgule pour Planning.csv).")
                all_files_uploaded = False
        else:
            all_files_uploaded = False
            st.info(f"Veuillez charger le fichier '{name}.csv'.")


    if all_files_uploaded and st.button("Générer le Menu et la Liste de Courses"):
        try:
            # Vérification des colonnes essentielles
            verifier_colonnes(dataframes["Recettes"], [COLONNE_NOM, COLONNE_ID_RECETTE, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Transportable", "Calories"], "Recettes.csv")
            verifier_colonnes(dataframes["Planning"], ["Date", "Participants", "Transportable", "Temps", "Nutrition"], "Planning.csv")
            verifier_colonnes(dataframes["Ingredients"], [COLONNE_NOM, COLONNE_ID_INGREDIENT, "Qte reste", "unité"], "Ingredients.csv")
            verifier_colonnes(dataframes["Ingredients_recettes"], [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingredients_recettes.csv")
            verifier_colonnes(dataframes["Menus"], ["Date", "Recette"], "Menus.csv")


            with st.spinner("Génération du menu en cours..."):
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
                liste_courses_df = pd.DataFrame(liste_courses.items(), columns=["Ingrédient", "Quantité manquante"])
                st.dataframe(liste_courses_df)

                # Option de téléchargement de la liste de courses
                csv = liste_courses_df.to_csv(index=False, sep=';', encoding='utf-8-sig') # utf-8-sig pour Excel
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
        except Exception as e:
            st.error(f"Une erreur inattendue est survenue lors de la génération: {e}")
            logger.exception("Erreur lors de la génération du menu dans Streamlit")

if __name__ == "__main__":
    main()
