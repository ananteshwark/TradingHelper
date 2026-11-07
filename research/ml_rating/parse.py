"""Each downloaded quarterly filing as one row of P&L lines in rupees.

Usage: parse.py FILES_DIR OUT_PARQUET
Old-format HTML pages (to late 2017) state their unit ('Rs. in lakhs' etc.); XBRL instances
are in rupees, the quarter's column being the context 'OneD'.
"""
import html
import json
import re
import sys
from pathlib import Path

import polars as pl

UNITS = {'lakh': 1e5, 'lac': 1e5, 'crore': 1e7, 'million': 1e6, 'thousand': 1e3}

# Old HTML: labels reduced to their letters and matched from the start by these regular
# expressions, in this order of preference; the first row that matches wins (the main
# table comes first). Some pages carry a zero 'before exceptional items' line, so for
# HTML that figure is rebuilt from profit before tax and exceptional items instead.
HTML = {
    'revenue': [r'totalincomefromoperations', r'[a-j]?netsalesincomefromoperation',
                r'totalrevenuefromoperations', r'[a-j]?revenuefromoperations',
                r'incomefromoperations', r'[a-j]?interestearned'],
    'other_income': [r'otherincome$', r'otherincome'],
    'finance_costs': [r'[a-j]?financecosts?$', r'(less)?interest$', r'[a-j]?interestexpended',
                      r'[a-j]?financecost', r'[a-j]?interestandfinancecharges'],
    'depreciation': [r'[a-j]?depreciation'],
    'exceptional': [r'exceptionalitems?$'],
    'pbt_pre_exc': [r'operatingprofitbeforeprovisions'],
    'pbt': [r'profitlossfromordinaryactivitiesbeforetax', r'profitlossbeforetax',
            r'profitbeforetax'],
    'pat': [r'netprofitlossfortheperiod$', r'netprofitlossfromordinaryactivitiesaftertax',
            r'profitlossfortheperiod$'],
    'eps': [r'[a-j]?basic$', r'basicepsbeforeextraordinaryitems',
            r'basicepsforcontinuedanddiscontinuedoperations',
            r'basicepsforcontinuingoperations', r'basic'],
    'paid_up': [r'paidupequitysharecapital'],
    'face_value': [r'facevalue'],
}
BANK_HINT = re.compile(r'interest earned', re.I)

XBRL = {
    'revenue': ['RevenueFromOperations', 'InterestEarned', 'TotalIncomeFromOperations'],
    'other_income': ['OtherIncome'],
    'finance_costs': ['FinanceCosts', 'InterestExpended'],
    'depreciation': ['DepreciationDepletionAndAmortisationExpense', 'DepreciationAndAmortisationExpense'],
    'pbt_pre_exc': ['ProfitBeforeExceptionalItemsAndTax', 'ProfitLossBeforeExceptionalItemsAndTax',
                    'OperatingProfitBeforeProvisionAndContingencies'],
    'pbt': ['ProfitBeforeTax', 'ProfitLossBeforeTax',
            'ProfitLossFromOrdinaryActivitiesBeforeTax'],
    'pat': ['ProfitLossForPeriod', 'ProfitLossForThePeriod',
            'ProfitLossFromOrdinaryActivitiesAfterTax'],
    'eps': ['BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations',
            'BasicEarningsLossPerShareFromContinuingOperations',
            'BasicEarningsPerShareAfterExtraordinaryItems',
            'BasicEarningsPerShareBeforeExtraordinaryItems'],
    'paid_up': ['PaidUpValueOfEquityShareCapital'],
    'exceptional': ['ExceptionalItemsBeforeTax', 'ExceptionalItems'],
    'face_value': ['FaceValueOfEquityShareCapital'],
}


def number(s):
    s = s.replace(',', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    s = s.strip('()').strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_html(text):
    unit = None
    m = re.search(r'Amount\s*\(\s*Rs\.?\s*in\s*([a-z]+)', text, re.I)
    if m:
        unit = next((v for k, v in UNITS.items() if m.group(1).lower().startswith(k)), None)
    elif re.search(r'Amount\s*\(\s*Rs\.?\s*\)', text, re.I):
        unit = 1.0
    rows = []
    for row in re.findall(r'<tr[^>]*>(.*?)</tr>', text, flags=re.S | re.I):
        cells = [re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', c))).strip()
                 for c in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S | re.I)]
        cells = [c for c in cells if c]
        # A row holds one line item, or several side by side: label, value, label, ...
        for i in range(0, len(cells) - 1, 2):
            rows.append((re.sub(r'[^a-z]', '', cells[i].lower()), cells[i + 1]))
    out = {'unit': unit, 'bank': bool(BANK_HINT.search(text))}
    for key, pats in HTML.items():
        for p in pats:
            hit = next((number(v) for lab, v in rows
                        if re.match(p, lab) and number(v) is not None), None)
            if hit is not None:
                out[key] = hit
                break
    if unit is None:
        return None
    if not out['bank'] and out.get('pbt') is not None:
        out['pbt_pre_exc'] = out['pbt'] - (out.get('exceptional') or 0.0)
    for k in ('revenue', 'other_income', 'finance_costs', 'depreciation', 'pbt_pre_exc',
              'pbt', 'pat', 'paid_up', 'exceptional'):
        if out.get(k) is not None:
            out[k] *= unit
    return out


def parse_xbrl(text):
    out = {'unit': 1.0, 'bank': 'InterestEarned' in text}
    for key, names in XBRL.items():
        for name in names:
            m = re.search(r'<[\w\-]+:%s\b[^>]*contextRef="OneD"[^>]*>([^<]*)<' % name, text) or \
                re.search(r'<[\w\-]+:%s\b[^>]*contextRef="OneI"[^>]*>([^<]*)<' % name, text)
            if m and number(m.group(1)) is not None:
                out[key] = number(m.group(1))
                break
    return out


rows = []
for d in sorted(Path(sys.argv[1]).iterdir()):
    meta_path = d / 'meta.json'
    if not meta_path.exists():
        continue
    for end, m in json.loads(meta_path.read_text()).items():
        f = d / m['file']
        if not f.exists():
            continue
        text = f.read_text(encoding='utf-8', errors='replace')
        got = parse_xbrl(text) if f.suffix == '.xml' else parse_html(text)
        if not got:
            continue
        rows.append({'sid': int(d.name), 'period_end': end, 'broadcast': m['broadcast'],
                     'format': f.suffix[1:], **{k: got.get(k) for k in
                     ('revenue', 'other_income', 'finance_costs', 'depreciation',
                      'pbt_pre_exc', 'pbt', 'pat', 'eps', 'paid_up', 'face_value', 'bank',
                      'exceptional')}})
df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
    pl.col('period_end').str.to_date(), pl.col('broadcast').str.to_datetime())
df.write_parquet(sys.argv[2])
print(df.height, 'filings;', df['sid'].n_unique(), 'companies')
print(df.select([pl.col(c).is_null().mean().alias(c) for c in
                 ('revenue', 'pbt_pre_exc', 'pbt', 'pat', 'eps', 'paid_up', 'face_value')]))
