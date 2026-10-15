"""Look and feel: theme-aware CSS and small presentation helpers.

Streamlit's own theme comes from .streamlit/config.toml [theme]. This module
adds what the theme cannot: spacing, softer cards, larger touch targets,
and a friendlier tone for headings. Keep everything here; app.py only calls
inject() once per run.
"""
from __future__ import annotations

import streamlit as st

CSS = """
:root {
  --fmb-surface: #FFFFFF;
  --fmb-border: #E4D9CA;
  --fmb-sand: #F3ECE2;
  --fmb-accent: #2F6B66;
  --fmb-accent-soft: rgba(47, 107, 102, 0.12);
  --fmb-muted: #6B625A;
  --fmb-shadow: 0 1px 2px rgba(59, 53, 48, 0.05);
  --fmb-font: "Nunito", "Source Sans 3", "Source Sans", "Segoe UI", system-ui, sans-serif;
}

/* Fallback font stack in case the web font does not load. */
html, body, [data-testid="stApp"], [data-testid="stApp"] input, [data-testid="stApp"] textarea,
[data-testid="stApp"] button, [data-testid="stApp"] [data-baseweb] { font-family: var(--fmb-font); }
[data-testid="stIconMaterial"] { font-family: "Material Symbols Rounded" !important; }
code, pre, kbd { font-family: "Source Code Pro", ui-monospace, monospace !important; }

/* Layout and breathing room */
[data-testid="stMainBlockContainer"] { padding: 2.5rem 2.5rem 5rem; max-width: 68rem; }
[data-testid="stAppDeployButton"], [data-testid="stMainMenu"] { display: none; }
[data-testid="stVerticalBlock"] { gap: 1rem; }
[data-testid="stMarkdownContainer"] p, [data-testid="stMarkdownContainer"] li { line-height: 1.6; }
[data-testid="stMarkdownContainer"] h1 { letter-spacing: -0.01em; padding-top: 0.5rem; padding-bottom: 0.75rem; }
[data-testid="stMarkdownContainer"] h2 { padding-top: 1rem; }
[data-testid="stMarkdownContainer"] h3 { padding-top: 0.5rem; }
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p { color: var(--fmb-muted) !important; }
[data-testid="stWidgetLabel"] p { font-weight: 600; }

/* Cards: expanders, forms, chat messages, uploader */
[data-testid="stExpander"] details, [data-testid="stForm"], [data-testid="stChatMessage"] {
  background: var(--fmb-surface); border: 1px solid var(--fmb-border);
  border-radius: 1rem; box-shadow: var(--fmb-shadow);
}
[data-testid="stForm"] { padding: 1.25rem 1.25rem 1rem; }
[data-testid="stExpander"] summary { padding: 0.85rem 1rem; min-height: 44px; font-weight: 600; }
[data-testid="stExpander"] summary:hover { color: var(--fmb-accent); }
[data-testid="stExpanderDetails"] { padding: 0 1rem 1rem; }
[data-testid="stChatMessage"] { padding: 1rem 1.1rem; }
[data-testid="stFileUploaderDropzone"] {
  background: var(--fmb-surface); border: 1.5px dashed var(--fmb-border); border-radius: 1rem;
}

/* Metrics read as small cards */
[data-testid="stMetric"] {
  background: var(--fmb-surface); border: 1px solid var(--fmb-border); border-radius: 1rem;
  padding: 1rem 1.1rem; box-shadow: var(--fmb-shadow);
}
[data-testid="stMetricLabel"] { color: var(--fmb-muted); }
[data-testid="stMetricLabel"] div, [data-testid="stMetricLabel"] p { white-space: normal; overflow: visible; text-overflow: clip; }
[data-testid="stMetricValue"] { color: var(--fmb-accent); font-size: 2rem; font-weight: 700; }

/* Tabs as gentle pills */
[data-testid="stTabs"] [role="tablist"] {
  display: flex; flex-wrap: wrap; gap: 0.35rem; background: var(--fmb-sand); padding: 0.3rem;
  border-radius: 1.5rem; width: fit-content; max-width: 100%;
}
[data-testid="stTab"] .react-aria-SelectionIndicator { display: none; }
[data-testid="stTab"] {
  border-radius: 999px; padding: 0.5rem 1rem; min-height: 40px; font-weight: 600;
  color: var(--fmb-muted); background: transparent; white-space: nowrap;
}
[data-testid="stTab"]:hover { color: var(--fmb-accent); background: rgba(255, 255, 255, 0.6); }
[data-testid="stTab"][aria-selected="true"] { background: var(--fmb-surface); color: var(--fmb-accent); box-shadow: var(--fmb-shadow); }

/* Buttons: comfortable touch targets, softer secondary style */
[data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button,
[data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a, [data-testid="stCameraInputButton"] {
  min-height: 44px; padding: 0.5rem 1.15rem; font-weight: 600; line-height: 1.3;
}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"], [data-testid="stBaseLinkButton-secondary"] {
  background: var(--fmb-surface); border-color: var(--fmb-border); color: var(--fmb-accent);
}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover,
[data-testid="stBaseLinkButton-secondary"]:hover { background: var(--fmb-accent-soft); border-color: var(--fmb-accent); }
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover { filter: brightness(1.08); }

/* Inputs */
[data-testid="stTextInputRootElement"], [data-testid="stTextAreaRootElement"], [data-testid="stNumberInputContainer"],
[data-testid="stSelectbox"] [role="group"], [data-testid="stTimeInput"] [role="group"], [data-testid="stDateInputField"] {
  background: var(--fmb-surface); border: 1px solid var(--fmb-border); border-radius: 0.75rem;
}
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stDateInput"] input { min-height: 44px; }
[data-testid="stChatInput"] { border-radius: 1rem; background: var(--fmb-surface); }

/* Alerts and tables */
[data-testid="stAlert"] { border-radius: 0.9rem; }
[data-testid="stAlert"] p { line-height: 1.5; }
[data-testid="stDataFrame"] { border-radius: 0.75rem; overflow: hidden; }

/* Sidebar as a calm side panel */
[data-testid="stSidebarContent"] { padding-top: 0.75rem; }
[data-testid="stSidebarUserContent"] { padding: 0.5rem 1.25rem 2rem; }
[data-testid="stSidebarNavItems"] { padding: 0 0.75rem; }
[data-testid="stSidebarNavLink"] { border-radius: 0.75rem; min-height: 44px; padding: 0.3rem 0.8rem; }
[data-testid="stSidebarNavLink"] span { font-size: 1rem; }
[data-testid="stSidebarNavLink"]:hover { background: rgba(255, 255, 255, 0.7); }
[data-testid="stSidebarNavLink"][aria-current="page"] { background: var(--fmb-accent-soft); }
[data-testid="stSidebarNavLink"][aria-current="page"] span { color: var(--fmb-accent); font-weight: 700; }
[data-testid="stSidebarNavSeparator"] { border-color: var(--fmb-border); margin: 0.5rem 0; }
[data-testid="stSidebar"] [data-testid="stExpander"] details, [data-testid="stSidebar"] [data-testid="stForm"] { background: var(--fmb-surface); }
[data-testid="stSidebar"] [data-testid="stBaseButton-secondary"] { background: var(--fmb-surface); }
[data-testid="stSidebarCollapseButton"] button, [data-testid="stExpandSidebarButton"] { min-width: 44px; min-height: 44px; }

/* Phones */
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"] { padding: 3.75rem 1rem 4rem; }
  [data-testid="stMarkdownContainer"] h1 { font-size: 1.55rem !important; line-height: 1.25; }
  [data-testid="stMarkdownContainer"] h2 { font-size: 1.25rem !important; }
  [data-testid="stMarkdownContainer"] h3 { font-size: 1.1rem !important; }
  [data-testid="stForm"] { padding: 1rem 1rem 0.75rem; }
  [data-testid="stMetric"] { padding: 0.8rem 1rem; }
  /* Metric cards sit two per row on a phone instead of stacking. */
  [data-testid="stColumn"]:has([data-testid="stMetric"]) { flex: 1 1 calc(50% - 0.5rem) !important; min-width: calc(50% - 0.5rem) !important; }
  [data-testid="stElementContainer"]:has(> [data-testid="stButton"]), [data-testid="stElementContainer"]:has(> [data-testid="stDownloadButton"]),
  [data-testid="stElementContainer"]:has(> [data-testid="stLinkButton"]), [data-testid="stElementContainer"]:has(> [data-testid="stFormSubmitButton"]),
  [data-testid="stButton"], [data-testid="stFormSubmitButton"], [data-testid="stDownloadButton"], [data-testid="stLinkButton"],
  [data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button,
  [data-testid="stDownloadButton"] button, [data-testid="stLinkButton"] a { width: 100% !important; }
}
"""


def inject() -> None:
    if CSS:
        st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)
