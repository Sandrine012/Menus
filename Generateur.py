import streamlit as st
import pandas as pd
import io

# ───────────────────────────────────────────────
# 1. OUTILS GÉNÉRIQUES
# ───────────────────────────────────────────────
def _csv_bytes(df: pd.DataFrame) -> bytes:
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


# ───────────────────────────────────────────────
# 2. CHARGER TOUTES LES DONNÉES NOTION ⇢ session_state
#    (appelé par le bouton « ⚙️ Charger les données »)
# ───────────────────────────────────────────────
def charger_donnees():
    # ⬇️  tes fonctions existantes : NE CHANGE RIEN
    st.session_state["df_ingredients"]              = get_ingredients_data()
    st.session_state["df_ingredients_recettes"]     = get_ingredients_recettes_data()
    st.session_state["df_recettes"]                 = get_recettes_data()

    # exemple : si tu as déjà un « df_menus_complet » quand tu génères
    # les menus, on ne le connaît pas encore ici. On prépare juste un
    # DataFrame vide : il sera rempli plus tard.
    st.session_state.setdefault("df_menus_gen", pd.DataFrame())

    st.success("✅ Données Notion chargées.")


# ───────────────────────────────────────────────
# 3. BLOC TÉLÉCHARGEMENT : 1 clic ⇒ 4 liens CSV
# ───────────────────────────────────────────────
def bloc_export():
    st.subheader("📥 Export CSV (4 fichiers)")

    keys = [
        "df_menus_gen",
        "df_ingredients",
        "df_ingredients_recettes",
        "df_recettes",
    ]

    # Vérifie que tout est bien là
    if not all(
        k in st.session_state and isinstance(st.session_state[k], pd.DataFrame)
        and not st.session_state[k].empty
        for k in keys
    ):
        st.info("Les données ne sont pas encore prêtes – clique d’abord sur « ⚙️ Charger les données ».")
        return

    if st.button("💾 Préparer les 4 fichiers CSV"):
        fichiers = {
            "Menus_generes.csv":        _csv_bytes(st.session_state["df_menus_gen"]),
            "Ingredients.csv":          _csv_bytes(st.session_state["df_ingredients"]),
            "Ingredients_recettes.csv": _csv_bytes(st.session_state["df_ingredients_recettes"]),
            "Recettes.csv":             _csv_bytes(st.session_state["df_recettes"]),
        }

        for nom, contenu in fichiers.items():
            st.download_button(
                label=f"Télécharger : {nom}",
                data=contenu,
                file_name=nom,
                mime="text/csv",
                key=nom,
            )
        st.success("Fichiers prêts ! Clique sur chaque lien pour les récupérer.")


# ───────────────────────────────────────────────
# 4. INTÉGRATION MINIMALE DANS **TON** main()
# ───────────────────────────────────────────────
def main():
    st.title("🍽️ Générateur de Menus Notion")

    # --- Bouton qui charge une bonne fois pour toutes les 3 bases Notion ---
    if st.button("⚙️ Charger les données"):
        with st.spinner("Connexion à Notion…"):
            charger_donnees()

    # --- Bloc export (apparaît seulement si les DataFrames sont présents) ---
    bloc_export()

    # … le reste **inchangé** de ton appli …


if __name__ == "__main__":
    main()
