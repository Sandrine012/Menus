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
        """
        Évalue la disponibilité et identifie les quantités manquantes pour une recette.
        Retourne: score_moyen_dispo, pourcentage_dispo, dict_ingredients_manquants {ing_id: qte_manquante}
        """
        ingredients_necessaires = self.calculer_quantite_necessaire(recette_id_str, nb_personnes)
        if not ingredients_necessaires: return 0, 0, {}

        total_ingredients_definis = len(ingredients_necessaires)
        ingredients_disponibles_compteur = 0
        score_total_dispo = 0
        ingredients_manquants = {} # NOUVEAU

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

            # NOUVEAU : Calcul des ingrédients manquants
            if qte_en_stock < qte_necessaire:
                quantite_manquante = qte_necessaire - qte_en_stock
                if quantite_manquante > 0: # S'assurer qu'il manque vraiment qqch
                    ingredients_manquants[ing_id_str] = quantite_manquante

            logger.debug(f"Ingr {ing_id_str} rec {recette_id_str}: stock={qte_en_stock}, nec={qte_necessaire}, ratio={ratio_dispo:.2f}, manquant={ingredients_manquants.get(ing_id_str, 0):.2f}")

        pourcentage_dispo = (ingredients_disponibles_compteur / total_ingredients_definis) * 100 if total_ingredients_definis > 0 else 0
        score_moyen_dispo = score_total_dispo / total_ingredients_definis if total_ingredients_definis > 0 else 0

        logger.debug(f"Éval recette {recette_id_str}: Score={score_moyen_dispo:.2f}, %Dispo={pourcentage_dispo:.0f}%")
        return score_moyen_dispo, pourcentage_dispo, ingredients_manquants

    def decrementer_stock(self, recette_id_str, nb_personnes, date_repas): # date_repas est accepté
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
                if qte_actuelle > 0 and qte_necessaire > 0: # On ne peut décrémenter que si on a du stock et besoin de l'ingrédient
                    qte_a_consommer = min(qte_actuelle, qte_necessaire) # On ne consomme pas plus que ce qu'on a
                    nouvelle_qte = qte_actuelle - qte_a_consommer
                    self.stock_simule.loc[idx, "Qte reste"] = nouvelle_qte


                    if qte_a_consommer > 0: # Si on a effectivement consommé quelque chose
                        ingredients_consommes_ids.add(ing_id_str)
                        logger.debug(f"Stock décrémenté pour {ing_id_str} (recette {recette_id_str}): {qte_actuelle:.2f} -> {nouvelle_qte:.2f} (consommé: {qte_a_consommer:.2f})")
            except (ValueError, KeyError) as e:
                logger.error(f"Erreur décrémentation stock pour {ing_id_str} (recette {recette_id_str}): {e}")


        self.anti_gaspi_ingredients = self._trouver_ingredients_stock_eleve() # Mettre à jour après décrémentation
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
            # Chercher dans le df_ingredients_initial qui contient les noms originaux
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
                valeur = str(self.df_recettes[self.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_page_id_str]["Transportable"].iloc[0]).strip().lower()
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
    def __init__(self, df_menus_hist):
        self.df_menus_historique = df_menus_hist.copy()

        # Conversion avec dayfirst pour dd/mm/YYYY (et heures si présentes)
        self.df_menus_historique["Date"] = pd.to_datetime(
            self.df_menus_historique["Date"],
            errors="coerce"
        )
        self.df_menus_historique.dropna(subset=["Date"], inplace=True)


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
        """
        Retourne True si la recette a déjà été planifiée entre
        date_actuelle - NB_JOURS_ANTI_REPETITION et
        date_actuelle + NB_JOURS_ANTI_REPETITION (inclus).
        """
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
            return not df_hist.loc[mask].empty

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
        recettes_scores_dispo = {} # {recette_id: score_disponibilite}
        recettes_ingredients_manquants = {} # {recette_id: {ing_id: qte_manquante}}

        nb_personnes = self.compter_participants(participants_str_codes)

        for recette_id_str_cand in self.recette_manager.df_recettes.index.astype(str):
            if str(transportable_req).strip().lower() == "oui" and not self.recette_manager.est_transportable(recette_id_str_cand):
                continue

            temps_total = self.recette_manager.obtenir_temps_preparation(recette_id_str_cand)
            if temps_req == "express" and temps_total > TEMPS_MAX_EXPRESS: continue
            if temps_req == "rapide" and temps_total > TEMPS_MAX_RAPIDE: continue

            if recette_id_str_cand in used_recipes_in_current_gen: continue
            if not self._filtrer_recette_base(recette_id_str_cand, participants_str_codes): continue
            if self.est_recente(recette_id_str_cand, date_repas): continue

            if nutrition_req == "equilibré":
                try:
                    if self.recette_manager.df_recettes.index.name == COLONNE_ID_RECETTE:
                        calories = int(self.recette_manager.df_recettes.loc[recette_id_str_cand, "Calories"])
                    else:
                        calories = int(self.recette_manager.df_recettes[self.recette_manager.df_recettes[COLONNE_ID_RECETTE].astype(str) == recette_id_str_cand]["Calories"].iloc[0])
                    if calories > REPAS_EQUILIBRE: continue
                except (KeyError, ValueError, TypeError, IndexError):
                    logger.debug(f"Calories non valides/trouvées pour {recette_id_str_cand} (filtre nutrition).")
                    continue

            # Utiliser la nouvelle méthode pour obtenir aussi les manquants
            score_dispo, _, manquants_pour_cette_recette = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_cand, nb_personnes)
            recettes_scores_dispo[recette_id_str_cand] = score_dispo
            recettes_ingredients_manquants[recette_id_str_cand] = manquants_pour_cette_recette # Stocker les manquants
            candidates.append(recette_id_str_cand)


            if self.recette_manager.recette_utilise_ingredient_anti_gaspi(recette_id_str_cand):
                anti_gaspi_candidates.append(recette_id_str_cand)

        if not candidates: return [], {} # Retourner aussi un dict vide pour les manquants

        candidates_triees = sorted(candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)
        anti_gaspi_triees = sorted(anti_gaspi_candidates, key=lambda r_id: recettes_scores_dispo.get(r_id, -1), reverse=True)

        if anti_gaspi_triees and recettes_scores_dispo.get(anti_gaspi_triees[0], -1) >= 0.5:
            return anti_gaspi_triees[:5], recettes_ingredients_manquants
        return candidates_triees[:10], recettes_ingredients_manquants


    def _traiter_menu_standard(self, date_repas, participants_str_codes, participants_count_int, used_recipes_current_gen_set, menu_recent_noms_list, transportable_req_str, temps_req_str, nutrition_req_str):
        # Modifié pour récupérer aussi le dictionnaire des ingrédients manquants
        recettes_candidates_initiales, recettes_manquants_dict = self.generer_recettes_candidates(
            date_repas, participants_str_codes, used_recipes_current_gen_set,
            transportable_req_str, temps_req_str, nutrition_req_str
        )
        if not recettes_candidates_initiales: return None, {} # Retourner None pour la recette, dict vide pour manquants

        recettes_historiques_semaine_set = self.recettes_meme_semaine_annees_precedentes(date_repas)
        scores_candidats_dispo = {
            r_id: self.recette_manager.evaluer_disponibilite_et_manquants(r_id, participants_count_int)[0]
            for r_id in recettes_candidates_initiales
        }
        preferred_candidates_list = [r_id for r_id in recettes_candidates_initiales if r_id in recettes_historiques_semaine_set]

        mots_cles_exclus_set = set()
        if menu_recent_noms_list:
            for nom_plat_recent in menu_recent_noms_list:
                if isinstance(nom_plat_recent, str) and nom_plat_recent.strip():
                    try: mots_cles_exclus_set.add(nom_plat_recent.lower().split()[0])
                    except IndexError: pass

        def get_first_word_local(recette_id_str_func):
            nom = self.recette_manager.obtenir_nom(recette_id_str_func)
            return nom.lower().split()[0] if nom and nom.strip() and "Recette_ID_" not in nom else ""


        recette_choisie_final = None
        if preferred_candidates_list:
            preferred_valides_motcle = [r_id for r_id in preferred_candidates_list if get_first_word_local(r_id) not in mots_cles_exclus_set]
            if preferred_valides_motcle:
                recette_choisie_final = sorted(preferred_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
            else: # Si le filtre mot-clé a tout enlevé des préférées
                recette_choisie_final = sorted(preferred_candidates_list, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]

        if not recette_choisie_final: # Si aucune préférée ou si on continue
            candidates_valides_motcle = [r_id for r_id in recettes_candidates_initiales if get_first_word_local(r_id) not in mots_cles_exclus_set]
            if candidates_valides_motcle:
                recette_choisie_final = sorted(candidates_valides_motcle, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]
            elif recettes_candidates_initiales: # Si le filtre mot-clé a tout enlevé, prendre la meilleure des initiales
                recette_choisie_final = sorted(recettes_candidates_initiales, key=lambda r_id: scores_candidats_dispo.get(r_id, -1), reverse=True)[0]

        if recette_choisie_final:
            return recette_choisie_final, recettes_manquants_dict.get(recette_choisie_final, {})
        return None, {}


    def _log_decision_recette(self, recette_id_str, date_repas, participants_str_codes):
        if recette_id_str is not None:
            nom_recette = self.recette_manager.obtenir_nom(recette_id_str)
            adaptee = self.recette_manager.est_adaptee_aux_participants(recette_id_str, participants_str_codes)
            temps_prep = self.recette_manager.obtenir_temps_preparation(recette_id_str)
            logger.debug(f"Décision rec {recette_id_str} ({nom_recette}): Adaptée={adaptee}, Temps={temps_prep} min")
        else:
            logger.warning(f"Aucune recette sélectionnée pour {date_repas.strftime('%d/%m/%Y')} - Participants: {participants_str_codes}")

    def _formater_ingredients_manquants(self, ingredients_manquants_dict):
        if not ingredients_manquants_dict:
            return ""

        formatted_list = []
        for ing_id, qte_manquante in ingredients_manquants_dict.items():
            nom_ing = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            # Try to get unit from stock_simule
            ing_stock_df = self.recette_manager.stock_simule[self.recette_manager.stock_simule[COLONNE_ID_INGREDIENT].astype(str) == str(ing_id)]
            unit = ing_stock_df["unité"].iloc[0] if not ing_stock_df.empty and "unité" in ing_stock_df.columns else "unité(s)"
            formatted_list.append(f"{nom_ing} ({qte_manquante:.2f} {unit})")
        return ", ".join(formatted_list)

    def generer_menu_repas_b(self, date_repas, plats_transportables_semaine_dict, repas_b_utilises_ids_list, menu_recent_noms_list):
        candidats_restes_ids = []
        sorted_plats_transportables = sorted(plats_transportables_semaine_dict.items(), key=lambda item: item[0])

        mots_cles_recents_set = set()
        if menu_recent_noms_list:
            for nom_plat_r in menu_recent_noms_list:
                if isinstance(nom_plat_r, str) and nom_plat_r.strip():
                    try: mots_cles_recents_set.add(nom_plat_r.lower().split()[0])
                    except IndexError: pass

        # Prioriser les restes
        for date_plat_orig, plat_id_orig_str in sorted_plats_transportables:
            jours_ecoules = (date_repas.date() - date_plat_orig.date()).days
            if 0 < jours_ecoules <= 2 and plat_id_orig_str not in repas_b_utilises_ids_list: # Reste bon pour 2 jours max
                nom_plat_reste = self.recette_manager.obtenir_nom(plat_id_orig_str)
                if nom_plat_reste and nom_plat_reste.strip() and "Recette_ID_" not in nom_plat_reste:
                    premier_mot_reste = nom_plat_reste.lower().split()[0]
                    if premier_mot_reste not in mots_cles_recents_set:
                        candidats_restes_ids.append(plat_id_orig_str)

        if candidats_restes_ids:
            plat_id_choisi_str = candidats_restes_ids[0]
            repas_b_utilises_ids_list.append(plat_id_choisi_str) # Marquer comme utilisé pour un repas B
            nom_plat_choisi_str = self.recette_manager.obtenir_nom(plat_id_choisi_str)
            logger.info(f"Repas B: Reste '{nom_plat_choisi_str}' choisi pour {date_repas.strftime('%d/%m/%Y %H:%M')}")
            # Pour les restes, on ne calcule pas les manquants, on suppose que tout était là.
            return nom_plat_choisi_str, plat_id_choisi_str, "Reste transportable utilisé", {}

        # Si pas de reste disponible, générer une nouvelle recette transportable
        logger.info(f"Repas B: Pas de reste disponible pour {date_repas.strftime('%d/%m/%Y %H:%M')}, génération d'une nouvelle recette transportable.")
        
        # Simuler un nombre de personnes (ex: 1 pour un repas B)
        nb_pers_b = self.compter_participants("B") # Devrait être 1
        
        # Obtenir toutes les recettes transportables candidates non utilisées récemment
        recettes_transportables_candidates_full, manquants_dict_all_candidates = self.generer_recettes_candidates(
            date_repas, "B", set(repas_b_utilises_ids_list), "oui", "", "" # Transportable OUI, temps/nutrition pas requis
        )

        # Filtrer par mots-clés récents pour les nouvelles recettes aussi
        filtered_candidates = [
            r_id for r_id in recettes_transportables_candidates_full
            if self.recette_manager.obtenir_nom(r_id).lower().split()[0] not in mots_cles_recents_set
        ]

        if filtered_candidates:
            # Pour les repas B, on prend la première candidate transportable la plus disponible
            recette_choisie_id = filtered_candidates[0]
            nom_plat_choisi_str = self.recette_manager.obtenir_nom(recette_choisie_id)
            repas_b_utilises_ids_list.append(recette_choisie_id) # Marquer comme utilisé pour un repas B
            logger.info(f"Repas B: Nouvelle recette '{nom_plat_choisi_str}' choisie pour {date_repas.strftime('%d/%m/%Y %H:%M')}")
            
            ingredients_manquants = manquants_dict_all_candidates.get(recette_choisie_id, {})
            remarques = "Nouvelle recette transportable générée"
            if ingredients_manquants:
                remarques += f" ({self._formater_ingredients_manquants(ingredients_manquants)})"
            
            return nom_plat_choisi_str, recette_choisie_id, remarques, ingredients_manquants
        else:
            logger.warning(f"Repas B: Aucune recette transportable générable trouvée pour {date_repas.strftime('%d/%m/%Y %H:%M')}.")
            return "Aucune recette B générable", None, "Aucune recette transportable générable trouvée", {}


    def _ajouter_resultat(self, resultats_liste, date_repas, nom_menu_str, participants_str_codes, remarques_str, temps_prep_int=0, recette_id_str_pour_eval=None):
        info_stock_str = ""
        # Utiliser la méthode evaluer_disponibilite_et_manquants pour les remarques de stock
        if recette_id_str_pour_eval:
            score_dispo, pourcentage_dispo, _ = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id_str_pour_eval, self.compter_participants(participants_str_codes))
            info_stock_str = f"Stock: {pourcentage_dispo:.0f}% des ingrédients disponibles (score: {score_dispo:.2f})"

        # Assurer que remarques_str est une chaîne avant concaténation
        remarques_finales = f"{remarques_str.strip()} {info_stock_str}".strip()

        resultats_liste.append({
            "Date": date_repas.strftime("%d/%m/%Y %H:%M"),
            COLONNE_NOM: nom_menu_str,
            "Participant(s)": participants_str_codes,
            "Remarques spécifiques": remarques_finales,
            "Temps de préparation": f"{temps_prep_int} min" if temps_prep_int else "-"
        })

    def generer_menu(self):
        resultats_df_list = []
        repas_b_utilises_ids = []
        plats_transportables_semaine = {}
        used_recipes_current_generation_set = set()
        menu_recent_noms = []
        ingredients_effectivement_utilises_ids_set = set() # Non utilisé pour la planification, mais utile
        self.ingredients_a_acheter_cumules = {} # Réinitialiser la liste de courses cumulée à chaque génération

        for _, repas_planning_row in self.df_planning.sort_values("Date").iterrows():
            date_repas_dt = repas_planning_row["Date"]
            participants_str = str(repas_planning_row["Participants"])
            participants_count = self.compter_participants(participants_str)
            transportable_req = str(repas_planning_row.get("Transportable", "")).strip().lower()
            temps_req = str(repas_planning_row.get("Temps", "")).strip().lower()
            nutrition_req = str(repas_planning_row.get("Nutrition", "")).strip().lower()

            logger.info(f"Traitement {date_repas_dt.strftime('%d/%m/%Y %H:%M')} - Participants: {participants_str}")

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = "" # Initialiser comme chaîne vide
            temps_prep_final = 0
            ingredients_manquants_pour_recette_choisie = {} # Initialiser comme dict vide

            if participants_str == "B":
                nom_plat_final, recette_choisie_id, remarques_repas, ingredients_manquants_pour_recette_choisie = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
                if recette_choisie_id:
                    used_recipes_current_generation_set.add(recette_choisie_id)
                    # Ajouter les ingrédients manquants (si la recette B est une nouvelle générée) à la liste de courses
                    for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                        self.ingredients_a_acheter_cumules[ing_id] = self.ingredients_a_acheter_cumules.get(ing_id, 0) + qte_manquante

                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, 1, date_repas_dt)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    
                    # Si c'est une nouvelle recette transportable générée (pas un reste), l'ajouter au pool pour les futurs repas B
                    if "Nouvelle recette transportable" in remarques_repas and date_repas_dt.weekday() < 5: # Uniquement les jours de semaine
                        plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                else:
                    remarques_repas = "Aucune option de repas B disponible."

            else: # Repas standard
                recette_choisie_id, ingredients_manquants_pour_recette_choisie = self._traiter_menu_standard(
                    date_repas_dt, participants_str, participants_count,
                    used_recipes_current_generation_set, menu_recent_noms,
                    transportable_req, temps_req, nutrition_req
                )
                if recette_choisie_id:
                    nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                    temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)
                    used_recipes_current_generation_set.add(recette_choisie_id)

                    # Mettre à jour la liste de courses cumulée avec les ingrédients manquants pour ce repas standard
                    for ing_id, qte_manquante in ingredients_manquants_pour_recette_choisie.items():
                        self.ingredients_a_acheter_cumules[ing_id] = self.ingredients_a_acheter_cumules.get(ing_id, 0) + qte_manquante
                    
                    # Construire la chaîne de remarques pour les repas standard
                    if ingredients_manquants_pour_recette_choisie:
                        remarques_repas = f"Ingrédients manquants: {self._formater_ingredients_manquants(ingredients_manquants_pour_recette_choisie)}"
                    else:
                        remarques_repas = "Tous les ingrédients sont en stock."

                    ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, participants_count, date_repas_dt)

                    # Ajouter les repas standard transportables au pool pour les futurs repas 'B'
                    if self.recette_manager.est_transportable(recette_choisie_id) and date_repas_dt.weekday() < 5: # Uniquement les jours de semaine
                        plats_transportables_semaine[date_repas_dt] = recette_choisie_id

                else: # Aucune recette choisie pour le repas standard
                    nom_plat_final = "Aucune recette générable"
                    remarques_repas = "Aucune recette trouvée avec les critères."

            # Mettre à jour menu_recent_noms pour influencer les choix de repas ultérieurs
            if nom_plat_final and "Erreur" not in nom_plat_final and "Aucune" not in nom_plat_final:
                menu_recent_noms.append(nom_plat_final)
                menu_recent_noms = menu_recent_noms[-5:] # Garder les 5 derniers repas pour le filtre

            # Enregistrer la décision
            self._log_decision_recette(recette_choisie_id, date_repas_dt, participants_str)

            # Ajouter le résultat à la liste
            self._ajouter_resultat(
                resultats_df_list, date_repas_dt, nom_plat_final, participants_str,
                remarques_repas, temps_prep_final, recette_choisie_id
            )

            # Supprimer les repas transportables trop anciens du pool (plus de 2 jours)
            dates_to_remove = [
                d for d in plats_transportables_semaine
                if (date_repas_dt.date() - d.date()).days > 2
            ]
            for d in dates_to_remove:
                del plats_transportables_semaine[d]
                logger.debug(f"Ancien reste supprimé du pool: {d.strftime('%d/%m/%Y')}")

        return pd.DataFrame(resultats_df_list)
