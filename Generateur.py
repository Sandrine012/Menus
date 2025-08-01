import streamlit as st
import pandas as pd
import io

# --------------------------------------------------
# 1.  Conversion DataFrame ➜ bytes pour Streamlit
# --------------------------------------------------
def _csv_bytes(df: pd.DataFrame) -> bytes:
    """
    Convertit un DataFrame en UTF-8 avec BOM (utf-8-sig) afin
    d’être lisible directement par Excel et Google Sheets.
    """
    buf = io.StringIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")
    return buf.getvalue().encode("utf-8-sig")


# --------------------------------------------------
# 2.  Bloc Streamlit à insérer dans l’interface
# --------------------------------------------------
def bloc_telechargement() -> None:
    """Affiche un bouton unique qui prépare 4 liens de téléchargement CSV."""
    st.header("📥 Export CSV (4 fichiers)")

    # Les quatre DataFrame attendus
    keys = [
        "df_menus_gen",
        "df_ingredients",
        "df_ingredients_recettes",
        "df_recettes",
    ]

    # Garde-fou : attend que les données soient prêtes
    if not all(
        k in st.session_state and isinstance(st.session_state[k], pd.DataFrame)
        and not st.session_state[k].empty
        for k in keys
    ):
        st.info("Les données ne sont pas encore prêtes – génère-les d’abord.")
        return

    # Affiche un seul bouton
    if st.button("💾 Préparer les 4 fichiers CSV"):
        fichiers = {
            "Menus_generes.csv":        _csv_bytes(st.session_state["df_menus_gen"]),
            "Ingredients.csv":          _csv_bytes(st.session_state["df_ingredients"]),
            "Ingredients_recettes.csv": _csv_bytes(st.session_state["df_ingredients_recettes"]),
            "Recettes.csv":             _csv_bytes(st.session_state["df_recettes"]),
        }

        # Quatre liens de téléchargement
        for nom, contenu in fichiers.items():
            st.download_button(
                label=f"Télécharger : {nom}",
                data=contenu,
                file_name=nom,
                mime="text/csv",
                key=nom,          # clé unique par bouton
            )
        st.success("Fichiers générés ! Clique sur chaque lien pour les récupérer.")


# --------------------------------------------------
# 3.  Exemple d’intégration dans main()
# --------------------------------------------------
def main():
    # 3-A.  ❱❱  TON PIPELINE EXISTANT  ❰❰
    # (fetch Notion ➜ transformation ➜ création DataFrame)
    # Exemple d’enregistrement dans session_state :
    #
    # st.session_state["df_recettes"] = df_recettes
    # st.session_state["df_ingredients"] = df_ingredients
    # st.session_state["df_ingredients_recettes"] = df_ing_recettes
    # st.session_state["df_menus_gen"] = df_menus_complet[["Date", "Participant(s)", "Nom"]]

    # 3-B.  Bloc export CSV
    bloc_telechargement()


if __name__ == "__main__":
    main()
