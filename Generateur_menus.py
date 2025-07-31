# @title
import pandas as pd
import random
import logging
from datetime import datetime, timedelta # Assurez-vous que timedelta est importé

# Constantes globales
NB_JOURS_ANTI_REPETITION = 42
FICHIER_RECETTES = "Recettes.csv"
FICHIER_PLANNING = "Planning.csv"
FICHIER_MENUS = "Menus.csv" # Pour l'historique
FICHIER_INGREDIENTS = "Ingredients.csv" # Assurez-vous que ce nom est correct
FICHIER_INGREDIENTS_RECETTES = "Ingredients_recettes.csv" # Assurez-vous que ce nom est correct
FICHIER_SORTIE_MENU_CSV = "Menu_genere.csv"
FICHIER_SORTIE_LISTES_TXT = "recapitulatif_ingredients.txt" # Pour toutes les listes

COLONNE_NOM = "Nom"
COLONNE_TEMPS_TOTAL = "Temps_total"
COLONNE_ID_RECETTE = "Page_ID" # Utilisé comme ID pour Recettes et Ingredients_recettes
COLONNE_ID_INGREDIENT = "Page_ID" # Utilisé comme ID pour Ingredients
COLONNE_AIME_PAS_PRINCIP = "Aime_pas_princip"

VALEUR_DEFAUT_TEMPS_PREPARATION = 10
TEMPS_MAX_EXPRESS = 20
TEMPS_MAX_RAPIDE = 30
REPAS_EQUILIBRE = 700

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')
logger = logging.getLogger(__name__)

def verifier_colonnes(df, colonnes_attendues, nom_fichier=""):
    colonnes_manquantes = [col for col in colonnes_attendues if col not in df.columns]
    if colonnes_manquantes:
        raise ValueError(f"Colonnes manquantes dans {nom_fichier}: {colonnes_manquantes}")

class RecetteManager:
    def __init__(self, df_recettes, df_ingredients, df_ingredients_recettes):
        self.df_recettes = df_recettes.copy()
        # S'assurer que Page_ID est l'index si la colonne existe
        if COLONNE_ID_RECETTE in self.df_recettes.columns and not self.df_recettes.index.name == COLONNE_ID_RECETTE:
            self.df_recettes = self.df_recettes.set_index(COLONNE_ID_RECETTE, drop=False)

        self.df_ingredients_initial = df_ingredients.copy() # Stock initial avec noms, etc. Ne sera pas modifié.
        self.df_ingredients_recettes = df_ingredients_recettes.copy()

        self.stock_simule = self.df_ingredients_initial.copy() # Copie pour la simulation
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

    # La méthode get_categories_recette est enlevée car la colonne "Catégorie" n'est plus supposée exister

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
            raise ValueError("La colonne 'Date' est manquante dans Planning.csv")

        verifier_colonnes(df_recettes, [COLONNE_ID_RECETTE, COLONNE_NOM, COLONNE_TEMPS_TOTAL, COLONNE_AIME_PAS_PRINCIP, "Type_plat", "Transportable"], "Recettes.csv")
        verifier_colonnes(df_ingredients, [COLONNE_ID_INGREDIENT, COLONNE_NOM, "Qte reste", "unité"], "Ingredients.csv")
        verifier_colonnes(df_ingredients_recettes, [COLONNE_ID_RECETTE, "Ingrédient ok", "Qté/pers_s"], "Ingredients_recettes.csv")
        verifier_colonnes(df_menus_hist, ["Date", "Nom_plat", "Type_plat", "Participants"], "Menus.csv")


        self.recette_manager = RecetteManager(df_recettes, df_ingredients, df_ingredients_recettes)
        self.menus_history_manager = MenusHistoryManager(df_menus_hist)

        self.liste_courses_cumulee = {} # Stocker les ingrédients manquants cumulés
        self.derniers_repas_generes_par_date = {} # Nouveau : Pour stocker les repas du jour

        # Historique des plats transportables générés dans la semaine en cours
        self.plats_transportables_semaine = {}

    def _obtenir_menu_recent_noms(self, date_repas, nb_jours=NB_JOURS_ANTI_REPETITION):
        date_limite = date_repas - timedelta(days=nb_jours)
        menu_recent = self.menus_history_manager.df_menus_historique[
            (self.menus_history_manager.df_menus_historique["Date"] >= date_limite) &
            (self.menus_history_manager.df_menus_historique["Date"] < date_repas) # Exclure le jour même
        ]
        return menu_recent["Nom_plat"].unique().tolist()

    def _calculer_nb_personnes(self, participants_str):
        if not isinstance(participants_str, str):
            return 1 # Valeur par défaut si non valide
        codes = [p.strip() for p in participants_str.split(',') if p.strip()]
        return len(codes)

    def _ajouter_aux_liste_courses(self, ingredients_manquants_pour_recette):
        for ing_id, qte_manquante in ingredients_manquants_pour_recette.items():
            nom_ingredient = self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)
            if nom_ingredient:
                self.liste_courses_cumulee[nom_ingredient] = self.liste_courses_cumulee.get(nom_ingredient, 0) + qte_manquante

    def generer_menu_repas_b(self, date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms):
        # Repas B: Reste des repas transportables des jours précédents
        # Chercher dans les repas transportables générés précédemment pour la semaine
        plats_disponibles_pour_b = []
        for d, plat_id in plats_transportables_semaine.items():
            # Assurez-vous que le repas est d'un jour précédent le repas B actuel
            if d < date_repas_dt and plat_id not in repas_b_utilises_ids:
                plats_disponibles_pour_b.append(plat_id)

        # Filtrer ceux déjà dans le menu récent
        plats_disponibles_pour_b_filtres = [
            plat_id for plat_id in plats_disponibles_pour_b
            if self.recette_manager.obtenir_nom(plat_id) not in menu_recent_noms
        ]

        if plats_disponibles_pour_b_filtres:
            recette_choisie_id = random.choice(plats_disponibles_pour_b_filtres)
            nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
            remarques_repas = "Reste du repas précédent."
            return nom_plat_final, recette_choisie_id, remarques_repas
        else:
            logger.warning(f"Pas de repas transportable précédent disponible pour le repas B du {date_repas_dt.strftime('%d/%m')}. Essai de trouver un plat classique.")
            # Si aucun reste, chercher un plat classique disponible (sans contrainte de temps)
            recettes_potentielles = self.recette_manager.df_recettes.copy()

            # Filtrer les plats non adaptés aux participants 'B' s'il y a lieu
            recettes_potentielles = recettes_potentielles[
                recettes_potentielles[COLONNE_ID_RECETTE].apply(lambda x: self.recette_manager.est_adaptee_aux_participants(x, "B"))
            ].copy()

            # Exclure les plats récemment faits
            recettes_potentielles = recettes_potentielles[
                ~recettes_potentielles[COLONNE_NOM].isin(menu_recent_noms)
            ].copy()

            # Prioriser les plats "anti-gaspi" s'il y en a et qu'ils ne sont pas transportables
            anti_gaspi_non_transportable = [
                r_id for r_id in recettes_potentielles[COLONNE_ID_RECETTE].tolist()
                if self.recette_manager.recette_utilise_ingredient_anti_gaspi(r_id) and
                   not self.recette_manager.est_transportable(r_id) # S'assurer qu'il n'est pas déjà classé comme transportable
            ]

            if anti_gaspi_non_transportable:
                recette_choisie_id = random.choice(anti_gaspi_non_transportable)
                nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                remarques_repas = "Plat généré (ingrédient anti-gaspi)."
                logger.info(f"Plat anti-gaspi choisi pour repas B : {nom_plat_final}")
                return nom_plat_final, recette_choisie_id, remarques_repas
            elif not recettes_potentielles.empty:
                # Si pas d'anti-gaspi non transportable, choisir un plat aléatoire parmi les restants
                recette_choisie_id = recettes_potentielles[COLONNE_ID_RECETTE].sample(1).iloc[0]
                nom_plat_final = self.recette_manager.obtenir_nom(recette_choisie_id)
                remarques_repas = "Plat généré (pas de reste disponible)."
                logger.info(f"Plat aléatoire choisi pour repas B (pas de reste/anti-gaspi) : {nom_plat_final}")
                return nom_plat_final, recette_choisie_id, remarques_repas
            else:
                logger.warning(f"Aucun plat classique disponible pour le repas B du {date_repas_dt.strftime('%d/%m')}.")
                return "Non défini (Repas B)", None, "Aucun plat disponible"


    def generer_menu(self):
        menu_genere_data = []
        df_planning_sorted = self.df_planning.sort_values(by='Date').copy()

        # Réinitialiser les plats transportables de la semaine à chaque génération de menu complet
        self.plats_transportables_semaine = {}
        self.liste_courses_cumulee = {} # Réinitialiser la liste de courses

        # Pour stocker les ID des repas B déjà utilisés comme "restes"
        repas_b_utilises_ids = []

        for index, row in df_planning_sorted.iterrows():
            date_repas_dt = row["Date"]
            participants_str = str(row["Participants"]).replace(" ", "") # Supprimer les espaces
            transportable_str = str(row["Transportable"]).strip().lower()
            temps_repas = str(row["Temps"]).strip().lower()
            nutrition_repas = str(row["Nutrition"]).strip().lower()
            nb_personnes = self._calculer_nb_personnes(participants_str)

            recette_choisie_id = None
            nom_plat_final = "Erreur - Plat non défini"
            remarques_repas = ""
            temps_prep_final = 0
            ingredients_consommes_ce_repas = []
            ingredients_manquants_pour_recette_choisie = {} # NOUVEAU

            # Récupérer le menu récent avant de choisir un plat pour ce repas
            menu_recent_noms = self._obtenir_menu_recent_noms(date_repas_dt)

            if participants_str == "B":
                nom_plat_final, recette_choisie_id, remarques_repas = self.generer_menu_repas_b(
                    date_repas_dt, plats_transportables_semaine, repas_b_utilises_ids, menu_recent_noms
                )
            else:
                # Logique pour les repas non "B"
                recettes_potentielles = self.recette_manager.df_recettes.copy()

                # Filtrer par participants qui n'aiment pas
                recettes_potentielles = recettes_potentielles[
                    recettes_potentielles[COLONNE_ID_RECETTE].apply(lambda x: self.recette_manager.est_adaptee_aux_participants(x, participants_str))
                ].copy()

                # Filtrer par type de plat si défini dans le planning
                if row.get("Type_plat") and str(row["Type_plat"]).strip() != "":
                    type_plat_planning = str(row["Type_plat"]).strip().lower()
                    recettes_potentielles = recettes_potentielles[
                        recettes_potentielles["Type_plat"].astype(str).str.lower().str.contains(type_plat_planning)
                    ].copy()

                # Filtrer par "Transportable" si requis
                if transportable_str == "oui":
                    recettes_potentielles = recettes_potentielles[
                        recettes_potentielles[COLONNE_ID_RECETTE].apply(self.recette_manager.est_transportable)
                    ].copy()
                    logger.debug(f"Après filtre transportable ({transportable_str}): {len(recettes_potentielles)} recettes")

                # Filtrer par temps de préparation
                if temps_repas == "express":
                    recettes_potentielles = recettes_potentielles[
                        recettes_potentielles[COLONNE_TEMPS_TOTAL] <= TEMPS_MAX_EXPRESS
                    ].copy()
                    logger.debug(f"Après filtre temps express ({TEMPS_MAX_EXPRESS}min): {len(recettes_potentielles)} recettes")
                elif temps_repas == "rapide":
                    recettes_potentielles = recettes_potentielles[
                        recettes_potentielles[COLONNE_TEMPS_TOTAL] <= TEMPS_MAX_RAPIDE
                    ].copy()
                    logger.debug(f"Après filtre temps rapide ({TEMPS_MAX_RAPIDE}min): {len(recettes_potentielles)} recettes")
                else: # Inclure les plats longs si aucune contrainte de temps
                    logger.debug("Pas de contrainte de temps spécifique pour ce repas.")


                # Exclure les plats récemment faits
                recettes_potentielles = recettes_potentielles[
                    ~recettes_potentielles[COLONNE_NOM].isin(menu_recent_noms)
                ].copy()
                logger.debug(f"Après filtre anti-répétition: {len(recettes_potentielles)} recettes")

                # Évaluer la disponibilité des ingrédients
                if not recettes_potentielles.empty:
                    recettes_evaluables = []
                    for _, recette_row in recettes_potentielles.iterrows():
                        recette_id = recette_row[COLONNE_ID_RECETTE]
                        score_dispo, pourcentage_dispo, manquants = self.recette_manager.evaluer_disponibilite_et_manquants(recette_id, nb_personnes)
                        recettes_evaluables.append({
                            "id": recette_id,
                            "nom": recette_row[COLONNE_NOM],
                            "score_dispo": score_dispo,
                            "pourcentage_dispo": pourcentage_dispo,
                            "manquants": manquants
                        })

                    # Priorisation :
                    # 1. Recettes avec le plus d'ingrédients en stock (score_dispo élevé)
                    # 2. Recettes "anti-gaspi" si le stock est élevé pour un ingrédient clé (même si score_dispo n'est pas parfait)
                    # 3. Recettes avec le plus haut pourcentage d'ingrédients disponibles
                    # 4. Si égalité, choisir aléatoirement

                    # Filtrer d'abord pour avoir un pourcentage d'ingrédients disponibles > 0
                    recettes_evaluables = [r for r in recettes_evaluables if r["pourcentage_dispo"] > 0]
                    if not recettes_evaluables:
                        logger.warning(f"Aucune recette avec ingrédients disponibles pour {date_repas_dt.strftime('%d/%m %H:%M')}, participants: {participants_str}")
                        nom_plat_final = "Non défini (Pas d'ingrédients)"
                        remarques_repas = "Aucune recette avec suffisamment d'ingrédients disponibles."
                    else:
                        # Tenter de trouver une recette "anti-gaspi"
                        anti_gaspi_recettes = [
                            r for r in recettes_evaluables
                            if self.recette_manager.recette_utilise_ingredient_anti_gaspi(r["id"])
                        ]

                        if anti_gaspi_recettes:
                            # Parmi les anti-gaspi, prendre celle avec le meilleur score de dispo
                            recette_choisie_info = max(anti_gaspi_recettes, key=lambda x: x["score_dispo"])
                            remarques_repas = "Plat choisi (anti-gaspi)."
                            logger.info(f"Plat anti-gaspi choisi: {recette_choisie_info['nom']} (Score: {recette_choisie_info['score_dispo']:.2f})")
                        else:
                            # Si pas d'anti-gaspi, choisir la recette avec le meilleur score de disponibilité
                            recette_choisie_info = max(recettes_evaluables, key=lambda x: x["score_dispo"])
                            remarques_repas = "Plat choisi (meilleure dispo)."
                            logger.info(f"Plat choisi (meilleure dispo): {recette_choisie_info['nom']} (Score: {recette_choisie_info['score_dispo']:.2f})")

                        recette_choisie_id = recette_choisie_info["id"]
                        nom_plat_final = recette_choisie_info["nom"]
                        ingredients_manquants_pour_recette_choisie = recette_choisie_info["manquants"]

                        # Décrémenter le stock et obtenir les ingrédients réellement consommés
                        ingredients_consommes_ce_repas = self.recette_manager.decrementer_stock(recette_choisie_id, nb_personnes, date_repas_dt)
                        temps_prep_final = self.recette_manager.obtenir_temps_preparation(recette_choisie_id)

                        # Ajouter à la liste de courses cumulée
                        self._ajouter_aux_liste_courses(ingredients_manquants_pour_recette_choisie)
                else:
                    nom_plat_final = "Non défini (Critères trop stricts)"
                    remarques_repas = "Aucune recette ne correspond aux critères de sélection (temps, transportable, etc.)."
                    logger.warning(f"Aucune recette pour {date_repas_dt.strftime('%d/%m %H:%M')} après tous les filtres initiaux.")

            # Gérer les plats transportables pour les repas B futurs
            if transportable_str == "oui" and recette_choisie_id and participants_str != "B":
                self.plats_transportables_semaine[date_repas_dt] = recette_choisie_id
                logger.debug(f"Ajouté {nom_plat_final} ({recette_choisie_id}) comme transportable pour {date_repas_dt}")
            elif participants_str == "B" and recette_choisie_id:
                # Si un repas B a été choisi parmi les transportables précédents, l'ajouter à la liste des repas B déjà utilisés
                if recette_choisie_id in self.plats_transportables_semaine.values():
                    repas_b_utilises_ids.append(recette_choisie_id)


            # Ajouter le repas généré au DataFrame final
            menu_genere_data.append({
                "Date": date_repas_dt.strftime('%d/%m/%Y %H:%M'),
                "Participants": participants_str,
                "Nom_plat": nom_plat_final,
                "Type_plat": row.get("Type_plat", ""), # Garder le type de plat du planning si défini
                "Transportable": transportable_str.capitalize(),
                "Temps_preparation_estime": temps_prep_final,
                "Nutrition_cible": nutrition_repas.capitalize(),
                "Remarques": remarques_repas,
                "Ingredients_manquants_pour_recette": ', '.join([
                    f"{self.recette_manager.obtenir_nom_ingredient_par_id(ing_id)} ({qte:.2f})"
                    for ing_id, qte in ingredients_manquants_pour_recette_choisie.items()
                ])
            })

        df_menu_genere = pd.DataFrame(menu_genere_data)
        return df_menu_genere, self.liste_courses_cumulee
