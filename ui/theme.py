"""Session-local presentation tokens; no data or calculation dependencies."""

MINT = '#50E3C2'
BLUE = '#4A90E2'
CORAL = '#FF6B6B'
AMBER = '#F5A623'
FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif'

LIGHT_CSS = '''
:root {
  color-scheme: light;
  --terminal-page: #f5f7fa; --terminal-surface: #ffffff;
  --terminal-raised: #f0f3f7; --terminal-ink: #263445;
  --terminal-muted: #637083; --terminal-line: #f0f2f6;
  --terminal-control-line: #ccd5df; --terminal-grid: #E5E7EB;
  --terminal-shadow: 0 4px 12px rgba(0,0,0,0.05);
  --terminal-link: #2866ab; --terminal-selection: #d4f7ed;
  --terminal-info-bg: #edf4fc; --terminal-info-ink: #285888;
  --terminal-success-bg: #e2f8f1; --terminal-success-ink: #17634f;
  --terminal-warning-bg: #fff5e4; --terminal-warning-ink: #805014;
  --terminal-error-bg: #fff0ee; --terminal-error-ink: #a63838;
  --terminal-table-filter: none;
}
'''

DARK_CSS = '''
:root {
  color-scheme: dark;
  --terminal-page: #13171d; --terminal-surface: #1E2329;
  --terminal-raised: #272e37; --terminal-ink: #f4f6f9;
  --terminal-muted: #aab5c4; --terminal-line: #2d333b;
  --terminal-control-line: #485464; --terminal-grid: #374151;
  --terminal-shadow: 0 4px 12px rgba(0,0,0,0.3);
  --terminal-link: #85baff; --terminal-selection: #235448;
  --terminal-info-bg: #1b3047; --terminal-info-ink: #b5d7ff;
  --terminal-success-bg: #183c33; --terminal-success-ink: #94e6cf;
  --terminal-warning-bg: #3a2e1c; --terminal-warning-ink: #f6cd89;
  --terminal-error-bg: #41272c; --terminal-error-ink: #ffb5b5;
  --terminal-table-filter: invert(.9) hue-rotate(180deg) brightness(1.25);
}
'''

CSS = '''
html, body, [data-testid="stApp"] {
  font-family: FONT_STACK !important;
  color: var(--terminal-ink); background: var(--terminal-page);
  scrollbar-color: var(--terminal-control-line) var(--terminal-page);
}
[data-testid="stApp"] :is(h1,h2,h3,h4,h5,h6,p,li,label,summary,pre,input,textarea,button,table,[role="tab"]) {
  font-family: FONT_STACK !important;
}
[data-testid="stAppViewContainer"], [data-testid="stMain"] {
  background: var(--terminal-page); color: var(--terminal-ink);
}
header[data-testid="stHeader"] {display: none !important;}
/* A collapsed sidebar still needs its native opener, including on phones. */
header[data-testid="stHeader"]:has([data-testid="stExpandSidebarButton"]),
header[data-testid="stHeader"] div:has([data-testid="stExpandSidebarButton"]) {display: contents !important;}
header [data-testid="stAppDeployButton"], header [data-testid="stMainMenu"],
header [data-testid="stToolbarActions"] {display: none !important;}
header [data-testid="stExpandSidebarButton"] {position: fixed; top: .5rem; left: .5rem; z-index: 1001;}
.block-container {
  max-width: min(1400px, 95%) !important;
  padding-top: 1rem !important; padding-bottom: 2rem !important;
}
/* The stylesheet has no visible content and must not leave a flex gap above the title. */
[data-testid="stMainBlockContainer"] > [data-testid="stVerticalBlock"] >
[data-testid="stElementContainer"]:first-child:has([data-testid="stMarkdownContainer"] > style:only-child) {display: none;}
[data-testid="stMain"] h1 {padding-top: 0 !important;}
[data-testid="stSidebar"] {background: var(--terminal-surface); border-right: 1px solid var(--terminal-line);}
/* Align the sidebar's first control with the main page title. */
[data-testid="stSidebar"] > div:first-child {padding-top: 1rem !important;}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap: .5rem !important;}
[data-testid="stSidebar"] button {margin-top: 0 !important;}
[data-testid="stSidebarUserContent"] {padding-top: 0 !important; margin-top: 0 !important;}
/* Recent Streamlit versions keep a separate header even when the app header is hidden. */
[data-testid="stSidebarHeader"] {position: absolute; top: 0; right: 0; height: 0; padding: 0;}
[data-testid="stSidebarHeader"] [data-testid="stLogoSpacer"] {display: none;}
[data-testid="stSidebarCollapseButton"] {position: absolute; top: 1rem; right: .5rem; z-index: 2;}
.st-key-btn_toggle_theme {padding-right: 1.5rem;}
h1, h2, h3, h4, h5, h6, p, li, label, summary, pre,
[data-testid="stMarkdownContainer"], [data-testid="stWidgetLabel"],
[data-testid="stRadioOption"], [role="tab"] {color: var(--terminal-ink);}
/* st.text renders a span with its own native color; include it in theme changes. */
[data-testid="stText"], [data-testid="stText"] span {color: var(--terminal-ink);}
h1 {font-size: 2rem !important; font-weight: 700 !important; letter-spacing: -.025em;}
[data-testid="stSidebar"] h1 {font-size: 1.5rem !important;}
h2, h3 {font-weight: 650 !important; letter-spacing: -.015em;}
h1, h2, h3 {line-height: 1.4 !important; overflow: visible;}
[data-testid="stHeading"], [data-testid="stHeadingWithActionElements"] {overflow: visible;}
p, li, [data-testid="stMarkdownContainer"] {overflow-wrap: anywhere;}
[data-testid="stVerticalBlock"], [data-testid="stColumn"] {min-width: 0;}
[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] p,
.st-key-sandbox_surface [data-testid="stCaptionContainer"] p,
.st-key-ai_summary_card [data-testid="stCaptionContainer"] p,
.st-key-ai_settings_panel [data-testid="stCaptionContainer"] p {color: var(--terminal-muted) !important; opacity: 1;}
[data-testid="stForm"], [data-testid="stMetric"], [data-testid="stPlotlyChart"],
.st-key-ai_summary_card, .st-key-sandbox_ai_card {
  background: var(--terminal-surface) !important;
  border: 1px solid var(--terminal-line); border-radius: 12px;
  box-shadow: var(--terminal-shadow);
}
[data-testid="stMetric"] {padding: 16px 20px;}
/* Reserve the native delta line across its row so one metric never protrudes. */
.st-key-sandbox_surface [data-testid="stHorizontalBlock"]:has([data-testid="stMetricDelta"]) [data-testid="stMetric"] {min-height: 8.5rem;}
[data-testid="stMetricValue"] {font-weight: 700; font-size: 2rem; font-variant-numeric: tabular-nums; letter-spacing: -.025em;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] p {color: var(--terminal-muted); font-size: .8125rem;}
[data-testid="stMetricDelta"] svg {display: none;}
[data-testid="stMetricDelta"] {color: var(--terminal-link); background: var(--terminal-info-bg);}
[data-testid="stPlotlyChart"] {padding: 10px 8px 2px; overflow: hidden;}
[data-testid="stExpander"] details, [data-testid="stExpanderDetails"],
[data-testid="stVerticalBlockBorderWrapper"] {background: var(--terminal-surface); border-color: var(--terminal-line); border-radius: 12px;}
[data-testid="stTabs"] [role="tablist"] {flex-wrap: wrap; gap: .3rem; border-bottom: 1px solid var(--terminal-control-line);}
[data-testid="stTabs"] [role="tab"] {white-space: normal; height: auto; padding: .7rem 1rem; font-weight: 600;}
[role="tab"][aria-selected="true"] {color: var(--terminal-link);}
[data-baseweb="tab-highlight"] {background: #50E3C2;}
[data-testid="stTextInputRootElement"], [data-testid="stNumberInputContainer"],
[data-testid="stDateInputField"], [data-testid="stDateInputFieldsScroller"],
[data-testid="stTextAreaRootElement"], [data-testid="stSelectbox"] [role="combobox"],
[data-baseweb="select"] > div,
input:not([type="checkbox"]):not([type="radio"]):not([type="range"]), textarea {
  background: var(--terminal-raised); color: var(--terminal-ink);
  border-color: var(--terminal-control-line); caret-color: var(--terminal-link);
}
input::placeholder, textarea::placeholder {color: var(--terminal-muted); opacity: 1;}
[data-testid="stDateInputField"] span, [data-baseweb="select"] span {color: var(--terminal-ink);}
[data-testid="stIconMaterial"] {color: var(--terminal-muted);}
/* Real keyed Streamlit containers scope these rules to holding cards. */
[class*="st-key-portfolio_item_"] [data-testid="stNumberInputContainer"],
[class*="st-key-portfolio_item_"] [data-testid="stTextInputRootElement"] {
  background-color: transparent !important;
  border: none !important;
  box-shadow: none !important;
}
[class*="st-key-portfolio_item_"] input {
  background-color: transparent !important;
  border: none !important;
  border-bottom: 1px solid #d1d5db !important;
  border-radius: 0 !important;
  padding-left: 0 !important;
}
[class*="st-key-portfolio_item_"] input:focus {
  border-bottom: 2px solid #4A90E2 !important;
}
[class*="st-key-portfolio_item_"] [data-testid="stNumberInputStepDown"],
[class*="st-key-portfolio_item_"] [data-testid="stNumberInputStepUp"] {
  background: transparent !important;
  color: var(--terminal-ink, var(--text-color)) !important;
}
[class*="st-key-portfolio_item_"] :is([data-testid="stNumberInputStepDown"], [data-testid="stNumberInputStepUp"]) svg,
[class*="st-key-portfolio_item_"] :is([data-testid="stNumberInputStepDown"], [data-testid="stNumberInputStepUp"]) svg path {
  fill: currentColor !important;
  color: inherit !important;
}
[class*="st-key-portfolio_item_"] [class*="_delete"] button {
  color: var(--terminal-error-ink);
}
button, [data-testid^="stBaseButton-"] {
  background: var(--terminal-surface); color: var(--terminal-ink);
  border-color: var(--terminal-control-line); border-radius: 8px;
}
button p {color: inherit;}
button [data-testid="stMarkdownContainer"], button [data-testid="stMarkdownContainer"] p {color: inherit;}
/* Stable widget keys are the primary hooks; testids cover older Streamlit DOMs. */
.st-key-btn_save button, .st-key-btn_sync button, .st-key-btn_run button,
.st-key-btn_toggle_theme button, .st-key-btn_download_daily button,
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button,
[data-testid="stFormSubmitButton"] button {border-radius: 8px; transition: all 0.2s ease-in-out;}
[data-testid="stButton"] button:hover:not(:disabled),
[data-testid="stDownloadButton"] button:hover:not(:disabled),
[data-testid="stFormSubmitButton"] button:hover:not(:disabled) {
  transform: translateY(-2px); box-shadow: 0 4px 8px rgba(0,0,0,0.15);
  border-color: #4A90E2; color: var(--terminal-ink); background: var(--terminal-raised);
}
.st-key-btn_save button, .st-key-btn_run button, .st-key-btn_analyze button,
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
  background: #50E3C2; color: #102e28; border-color: #50E3C2; font-weight: 650;
}
.st-key-btn_save button:hover:not(:disabled), .st-key-btn_run button:hover:not(:disabled),
.st-key-btn_analyze button:hover:not(:disabled),
[data-testid="stBaseButton-primary"]:hover:not(:disabled),
[data-testid="stBaseButton-primaryFormSubmit"]:hover:not(:disabled) {background: #73ebd0; border-color: #73ebd0; color: #102e28;}
.st-key-btn_toggle_theme button {border-color: var(--terminal-control-line); width: 100%;}
button:disabled {opacity: .5; cursor: not-allowed;}
button:focus-visible, a:focus-visible, input:focus-visible, [role="combobox"]:focus-visible {outline: 3px solid #4A90E2; outline-offset: 3px;}
a {color: var(--terminal-link); text-underline-offset: 3px;}
hr, .scan-table td, .scan-table th {border-color: var(--terminal-control-line);}
.evidence {white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; line-height: 1.7;}
.scan-table {width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 14px;}
.scan-table td, .scan-table th {padding: 8px; border: 1px solid var(--terminal-control-line); overflow-wrap: anywhere;}
.scan-table th {background: var(--terminal-raised);}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentInfo"]) {background: var(--terminal-info-bg);}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentSuccess"]) {background: var(--terminal-success-bg);}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentWarning"]) {background: var(--terminal-warning-bg);}
[data-testid="stAlertContainer"]:has([data-testid="stAlertContentError"]) {background: var(--terminal-error-bg);}
[data-testid="stAlertContentInfo"], [data-testid="stAlertContentInfo"] p {color: var(--terminal-info-ink);}
[data-testid="stAlertContentSuccess"], [data-testid="stAlertContentSuccess"] p {color: var(--terminal-success-ink);}
[data-testid="stAlertContentWarning"], [data-testid="stAlertContentWarning"] p {color: var(--terminal-warning-ink);}
[data-testid="stAlertContentError"], [data-testid="stAlertContentError"] p {color: var(--terminal-error-ink);}
.st-key-ai_summary_card p[style*="background:"] {background: var(--terminal-info-bg) !important; color: var(--terminal-info-ink) !important;}
.st-key-ai_summary_card p[style*="background:#e9f5ed"] {background: var(--terminal-success-bg) !important; color: var(--terminal-success-ink) !important;}
.st-key-ai_summary_card p[style*="background:#fff4d6"] {background: var(--terminal-warning-bg) !important; color: var(--terminal-warning-ink) !important;}
.st-key-sandbox_ai_card .sandbox-coach-section {background: var(--terminal-raised) !important; color: var(--terminal-ink) !important;}
[data-testid="stTooltipContent"], [role="listbox"], [role="option"],
[data-testid="stDateInputCalendar"], [data-baseweb="popover"], [data-baseweb="calendar"],
[data-rac][role="dialog"], [data-rac][role="grid"], [data-testid="stJson"] {background: var(--terminal-raised); color: var(--terminal-ink); border-color: var(--terminal-line);}
[data-testid="stDateInputCalendar"] button, [data-testid="stDateInputCalendar"] th,
[data-testid="stDateInputCalendar"] abbr, [data-testid="stDateInputCalendar"] [role="columnheader"] {color: var(--terminal-ink);}
[data-testid="stDateInputCalendar"] [data-outside-month], [data-testid="stDateInputCalendar"] [data-disabled] {color: var(--terminal-muted);}
[data-testid="stDataFrame"] canvas {filter: var(--terminal-table-filter);}
[data-testid="stDataFrame"] {background: var(--terminal-surface); border-color: var(--terminal-line);}
[data-testid="stDataFrame"] [data-testid="stElementToolbar"] {background: var(--terminal-raised);}
::selection {background: var(--terminal-selection); color: var(--terminal-ink);}
@media(max-width: 640px) {
  /* Keep the mobile sidebar opener clear of the title. */
  .block-container {padding-top: 3.5rem !important; padding-left: 1rem !important; padding-right: 1rem !important;}
  .block-container h1 {font-size: 1.6rem !important;}
  [data-testid="stMetric"] {padding: 12px 14px;}
}
@media(prefers-reduced-motion: reduce) {
  [data-testid="stButton"] button, [data-testid="stDownloadButton"] button,
  [data-testid="stFormSubmitButton"] button {transition: none !important; transform: none !important;}
}
'''.replace('FONT_STACK', FONT)


def theme_css(is_dark_mode: bool):
    """Inject one complete stylesheet so light and dark rules never compete."""
    return '<style>' + (DARK_CSS if is_dark_mode else LIGHT_CSS) + CSS + '</style>'


def apply_plotly_theme(fig, is_dark_mode: bool):
    """Style an existing figure without changing its values or trace semantics."""
    ink = '#FFFFFF' if is_dark_mode else '#374151'
    grid = '#374151' if is_dark_mode else '#E5E7EB'
    fig.update_layout(
        template='plotly_dark' if is_dark_mode else 'plotly_white',
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        margin=dict(l=15, r=15, t=35, b=15),
        font=dict(family=FONT, color=ink, size=12),
        colorway=[MINT, BLUE, CORAL, AMBER],
        hoverlabel=dict(bgcolor='#1E2329' if is_dark_mode else '#ffffff', font_color=ink),
        legend=dict(font=dict(color=ink)),
    )
    fig.update_xaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid, automargin=True)
    fig.update_yaxes(gridcolor=grid, zerolinecolor=grid, linecolor=grid, automargin=True)
    fig.update_traces(line=dict(width=2.5), selector=dict(type='scatter'))
    fig.update_traces(line=dict(width=2.5), selector=dict(type='scattergl'))
    return fig
