import streamlit as st
import pandas as pd
import time, logging, httpx
from datetime import datetime
from notion_client import Client
from notion_client.errors import RequestTimeoutError, APIResponseError

# ────── CONFIG LOG ──────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ────── SECRETS NOTION ──────────────────────────────
NOTION_API_KEY           = st.secrets["notion_api_key"]
ID_RECETTES              = st.secrets["notion_database_id_recettes"]
ID_MENUS                 = st.secrets["notion_database_id_menus"]
ID_INGREDIENTS           = st.secrets["notion_database_id_ingredients"]
ID_INGREDIENTS_RECETTES  = st.secrets["notion_database_id_ingredients_recettes"]

notion = Client(auth=NOTION_API_KEY)

# ────── CONSTANTES CSV & PAGINATION ─────────────────
BATCH_SIZE, MAX_RETRY, WAIT_S = 50, 3, 5
SAISON_FILTRE = "Printemps"

CSV_RECETTES             = "Recettes.csv"
CSV_MENUS                = "Menus.csv"
CSV_INGREDIENTS          = "Ingredients.csv"
CSV_INGREDIENTS_RECETTES = "Ingredients_recettes.csv"

# ────── OUTIL GÉNÉRIQUE DE PAGINATION ───────────────
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

# ────── EXTRACTION : RECETTES ───────────────────────
HDR_RECETTES = ["Page_ID","Nom","ID_Recette","Saison",
                "Calories","Proteines","Temps_total",
                "Aime_pas_princip","Type_plat","Transportable"]
MAP_REC = {
    "Nom":("Nom_plat","title"), "ID_Recette":("ID_Recette","uid"),
    "Saison":("Saison","ms"),   "Calories":("Calories Recette","roll"),
    "Proteines":("Proteines Recette","roll"),
    "Temps_total":("Temps_total","form"), "Aime_pas_princip":("Aime_pas_princip","rollstr"),
    "Type_plat":("Type_plat","ms"), "Transportable":("Transportable","selcb")
}
def prop_val(p,k):
    if not p: return ""
    t = p["type"]
    if k=="title":   return "".join(x["plain_text"] for x in p["title"])
    if k=="uid":     u=p["unique_id"]; pr,nu=u.get("prefix"),u.get("number"); return f"{pr}-{nu}" if pr else str(nu or "")
    if k=="ms":      return ", ".join(o["name"] for o in p["multi_select"])
    if k=="roll":    return str(p["rollup"].get("number") or "")
    if k=="form":    fo=p["formula"]; return str(fo.get("number") or fo.get("string") or "")
    if k=="rollstr": return ", ".join(it["formula"].get("string") or "." for it in p["rollup"]["array"])
    if k=="selcb":   return "Oui" if (t=="select" and (p["select"] or {}).get("name","").lower()=="oui") or (t=="checkbox" and p["checkbox"]) else ""
    return ""
def extract_recettes():
    filt = {"and":[
        {"property":"Elément parent","relation":{"is_empty":True}},
        {"or":[
            {"property":"Saison","multi_select":{"contains":"Toute l'année"}},
            {"property":"Saison","multi_select":{"contains":SAISON_FILTRE}},
            {"property":"Saison","multi_select":{"is_empty":True}}]},
        {"or":[
            {"property":"Type_plat","multi_select":{"contains":"Salade"}},
            {"property":"Type_plat","multi_select":{"contains":"Soupe"}},
            {"property":"Type_plat","multi_select":{"contains":"Plat"}}]}]}
    rows=[]
    for p in paginate(ID_RECETTES, filter=filt):
        pr=p["properties"]; row=[p["id"]]
        for col in HDR_RECETTES[1:]:
            key,kind=MAP_REC[col]; row.append(prop_val(pr.get(key),kind))
        rows.append(row)
    return pd.DataFrame(rows,columns=HDR_RECETTES)

# ────── EXTRACTION : MENUS ───────────────────────────
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

# ────── EXTRACTION : INGRÉDIENTS ─────────────────────
HDR_INGR = ["Page_ID","Nom","Type de stock","unité","Qte reste"]
def extract_ingredients():
    rows=[]
    for p in paginate(ID_INGREDIENTS,
            filter={"property":"Type de stock","select":{"equals":"Autre type"}}):
        pr=p["properties"]
        # unité : peut être rich_text ou select ou absent
        u_prop = pr.get("unité",{})
        if u_prop.get("type")=="rich_text":
            unite="".join(t["plain_text"] for t in u_prop["rich_text"])
        elif u_prop.get("type")=="select":
            unite=(u_prop["select"] or {}).get("name","")
        else:
            unite=""
        qte_prop = pr.get("Qte reste", {})
        qte = ""
        if qte_prop.get("type") == "formula":
            formula_result = qte_prop.get("formula", {})
            if formula_result.get("type") == "number":
                qte = formula_result.get("number")
        rows.append([
            p["id"],
            "".join(t["plain_text"] for t in pr["Nom"]["title"]),
            (pr["Type de stock"]["select"] or {}).get("name",""),
            unite,
            str(qte or "")
        ])
    return pd.DataFrame(rows,columns=HDR_INGR)

# ────── EXTRACTION : INGRÉDIENTS ↔ RECETTES ──────────
HDR_IR = ["Page_ID","Qté/pers_s","Ingrédient ok","Type de stock f"]
def extract_ingr_rec():
    rows=[]
    for p in paginate(ID_INGREDIENTS_RECETTES,
            filter={"property":"Type de stock f","formula":{"string":{"equals":"Autre type"}}}):
        pr=p["properties"]
        parent = pr.get("Elément parent",{})
        pid = ""
        if parent and parent["type"]=="relation" and parent["relation"]:
            pid = parent["relation"][0]["id"]
        if not pid:
            pid = p["id"]
        qte = pr["Qté/pers_s"]["number"]
        if qte and qte>0:
            rows.append([
                pid,
                str(qte),
                ", ".join(r["id"] for r in pr["Ingrédient ok"]["relation"]),
                pr["Type de stock f"]["formula"]["string"] or ""
            ])
    return pd.DataFrame(rows,columns=HDR_IR)

# ────── UI STREAMLIT ────────────────────────────────
st.set_page_config(page_title="Exports Notion (4 CSV)", layout="centered")
st.title("📋 Exports Notion : Recettes • Menus • Ingrédients • Liens")

def bouton(label, func, csv_name):
    if st.button(label):
        with st.spinner("Extraction en cours…"):
            df = func()
        if df.empty:
            st.error("Aucune ligne trouvée (vérifiez ID & droits).")
        else:
            st.success(f"{len(df)} lignes extraites.")
            st.dataframe(df, use_container_width=True)
            st.download_button("📥 "+csv_name,
                               df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig"),
                               file_name=csv_name,
                               mime="text/csv")

bouton("Extraire les recettes",            extract_recettes,          CSV_RECETTES)
st.divider()
bouton("Extraire les menus",               extract_menus,             CSV_MENUS)
st.divider()
bouton("Extraire les ingrédients",         extract_ingredients,       CSV_INGREDIENTS)
st.divider()
bouton("Extraire ingrédients-recettes",    extract_ingr_rec,          CSV_INGREDIENTS_RECETTES)

st.info("Chaque bouton interroge uniquement la base concernée et produit un CSV conforme à vos modèles (UTF-8-SIG).")

