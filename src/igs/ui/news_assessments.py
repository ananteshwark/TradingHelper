"""Read-only view of persisted AI interpretations, including evidence and provenance."""
import streamlit as st

from igs.news import assessment_results
from igs.timeutil import IST
from igs.ui import auth


@st.cache_data(ttl=30, max_entries=4, show_spinner=False)
def load(_conn, kind='All'):
    return assessment_results(_conn, kind=None if kind == 'All' else kind)


def render(conn):
    auth.require_access()
    st.subheader('AI news assessments')
    st.caption('Stored AI output for the 200 most recent company assessments. '
               'Impact and tone range from −1 to +1; they are not predicted price returns. '
               'Confidence describes the evidence and reasoning, not the chance of profit. '
               'Viewing this section makes no paid AI requests.')
    if st.button('Refresh assessments', key='news_assessment_refresh'):
        load.clear()
    left, right = st.columns(2)
    kind = left.selectbox('Assessment type', ['All', 'Geopolitical impact', 'Stock news sentiment'],
                          key='news_assessment_type')
    rows = load(conn, kind)
    if not rows:
        st.info('No AI news assessments stored yet. Collected articles and their assessment '
                'status appear below; completed AI output will appear here.')
        return
    selected = rows
    companies = ['All', *sorted({r['company'] for r in selected})]
    company = right.selectbox('Company', companies, key='news_assessment_company')
    selected = [r for r in selected if company == 'All' or r['company'] == company]
    if not selected:
        st.info('No stored assessments match these filters.')
        return
    st.dataframe([{'Article': r['title'], 'Company': r['company'], 'Type': r['kind'],
                   'Impact / tone': round(float(r['impact']), 3),
                   'Confidence': f"{float(r['confidence']):.0%}",
                   'Assessed (IST)': r['assessed_at'].astimezone(IST).strftime('%d %b %Y %H:%M'),
                   'Model': r['model']} for r in selected], hide_index=True)
    by_key = {r['assessment_key']: r for r in selected}
    key = st.selectbox('Read full AI assessment', list(by_key), key='news_assessment_detail',
        format_func=lambda k: f"{by_key[k]['title']} — {by_key[k]['company']} "
                              f"({by_key[k]['kind']})")
    row = by_key[key]
    with st.container(border=True):
        st.subheader(row['title'])
        st.caption(f"{row['company']} · {row['kind']}")
        impact = float(row['impact'])
        direction = 'Positive' if impact > 0 else 'Negative' if impact < 0 else 'Neutral'
        cols = st.columns(3)
        cols[0].metric('AI direction', direction)
        cols[1].metric('Impact / tone', f'{impact:+.2f}')
        cols[2].metric('AI confidence', f"{float(row['confidence']):.0%}")
        st.markdown('**AI reasoning**')
        st.text(row['rationale'])
        st.markdown('**Quoted source evidence**')
        st.text(row['evidence'])
        st.caption(f"Channel: {row['channel']} · Model: {row['model']} · "
                   f"Prompt version: {row['prompt_version']}")
        st.caption(f"Published: {row['published_at'].astimezone(IST):%d %b %Y %H:%M IST} · "
                   f"Assessed: {row['assessed_at'].astimezone(IST):%d %b %Y %H:%M IST}")
        if row['company_id'] is None:
            st.info('The company named by AI is not matched to the instrument master. '
                    'This assessment is not linked to a stock rating.')
        if (row['url'] or '').startswith('https://'):
            st.link_button('Read original news', row['url'])
        with st.expander('Article text supplied to AI'):
            st.text(row['article_text'])
