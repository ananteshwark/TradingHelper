"""Admin model routing and private provider-key controls."""
from __future__ import annotations

import json
import os

import streamlit as st

from igs import envfile, settings
from igs.assistant import catalog
from igs.assistant.providers import PROVIDERS
from igs.config import ModelRoute, load_assistant
from igs.ui import auth


@auth.admin_action
def _key(provider, remove=False):
    name = PROVIDERS[provider][1]
    if remove:
        envfile.unset(envfile.default_path(), name)
    else:
        value = st.session_state[f'model_key_{provider}'].strip()
        envfile.set_value(envfile.default_path(), name, value)
    st.session_state[f'model_key_{provider}'] = ''


def render(cfg, editable):
    st.subheader('Providers and models by task')
    st.caption('Choose a provider and model separately for each task. Default uses the '
               'Claude model below. API billing is separate from chat subscriptions. '
               'Only the selected provider receives that task’s inputs. Effort settings below '
               'apply to supported Claude models; other providers use their default reasoning.')
    with st.expander('Additional provider API keys'):
        for provider, (label, variable, _) in PROVIDERS.items():
            if provider == 'anthropic':
                continue  # Existing Anthropic controls remain below.
            st.write(f'{label}: {"key configured" if os.environ.get(variable) else "no key"}')
            value = st.text_input(f'{label} API key', type='password',
                                  key=f'model_key_{provider}', disabled=not editable)
            a, b = st.columns(2)
            a.button(f'Save {label} key', key=f'model_save_key_{provider}',
                     disabled=not editable or not value, on_click=_key, args=(provider,))
            b.button(f'Remove {label} key', key=f'model_remove_key_{provider}',
                     disabled=not editable or not os.environ.get(variable),
                     on_click=_key, args=(provider, True))
    signature = json.dumps({'routes': {k: v.model_dump() for k, v in cfg.routes.items()},
                            'prices': {k: v.model_dump() for k, v in
                                       cfg.prices_usd_per_mtok.items()}}, sort_keys=True)
    if st.session_state.get('model_saved_signature') != signature:
        for task in catalog.TASK_LABELS:
            route = cfg.routes.get(task)
            st.session_state[f'model_route_{task}'] = (
                f'{route.provider}:{route.model}' if route else 'Default')
        for key in list(st.session_state):
            if str(key).startswith(('price_input_', 'price_output_', 'price_confirm_')):
                del st.session_state[key]
        st.session_state['model_saved_signature'] = signature
    data = catalog.read()
    if st.button('Refresh available models', key='model_catalog_refresh', disabled=not editable):
        with st.spinner('Loading provider catalogs (no inference charges)…'):
            data = catalog.refresh(force=True)
    st.caption('The server refreshes catalogs every six hours. New releases appear after '
               'the provider lists them for your API account. Saved assignments never '
               'switch automatically. Non-text models are filtered where identifiable; '
               'availability alone does not guarantee task compatibility.')
    options = {'Default'}
    for provider, entry in data.items():
        if provider not in PROVIDERS:
            continue
        options.update(f'{provider}:{m["id"]}' for m in entry.get('models', []))
        if entry.get('error'):
            st.warning(f'{PROVIDERS[provider][0]} refresh failed: {entry["error"]}. '
                       'Previous model choices are retained.')
        elif entry.get('updated_at'):
            st.caption(f'{PROVIDERS[provider][0]}: {len(entry.get("models", []))} models; '
                       f'updated {entry["updated_at"]}')
    options.update(f'anthropic:{m}' if ':' not in m else m for m in cfg.prices_usd_per_mtok)
    options.update(f'{r.provider}:{r.model}' for r in cfg.routes.values())
    choices = ['Default', *sorted(options-{'Default'})]
    routes = {}
    for task, label in catalog.TASK_LABELS.items():
        route = cfg.routes.get(task)
        current = f'{route.provider}:{route.model}' if route else 'Default'
        selected = st.selectbox(label, choices, index=choices.index(current),
            key=f'model_route_{task}', disabled=not editable, accept_new_options=True,
            help='You can enter provider:model-id if a new model is not listed yet.')
        if selected != 'Default':
            try:
                provider, model = selected.split(':', 1)
                routes[task] = ModelRoute(provider=provider, model=model)
            except ValueError:
                st.error(f'{label}: use provider:model-id with one of '
                         + ', '.join(PROVIDERS))
                return
    st.caption('Set current USD prices per million tokens for every assigned model. '
               'Newly listed models are not usable until pricing is saved. Non-Claude '
               'cost estimates include cached input at the full input rate and include '
               'reported reasoning tokens; provider invoices remain authoritative.')
    unique = {f'{r.provider}:{r.model}': r for r in routes.values()}
    with st.form('task_model_settings'):
        prices = dict(cfg.prices_usd_per_mtok)
        confirmed = True
        for identifier, route in unique.items():
            price = cfg.price_for(route)
            st.markdown(f'**{identifier}**')
            a, b = st.columns(2)
            input_price = a.number_input('Input USD / million tokens', min_value=0.0,
                value=float(price.input) if price else 0.0, format='%.6f',
                key=f'price_input_{identifier}', disabled=not editable)
            output_price = b.number_input('Output USD / million tokens', min_value=0.0,
                value=float(price.output) if price else 0.0, format='%.6f',
                key=f'price_output_{identifier}', disabled=not editable)
            checked = st.checkbox('I have checked these prices (zero only for a free model)',
                value=price is not None, key=f'price_confirm_{identifier}', disabled=not editable)
            confirmed = confirmed and checked
            prices[identifier] = {'input': input_price, 'output': output_price}
        saved = st.form_submit_button('Save task models', disabled=not editable)
    if saved and editable:
        if not confirmed:
            st.error('Confirm pricing for each new model before saving.')
            return
        values = load_assistant().model_dump()
        values['routes'] = {task: route.model_dump() for task, route in routes.items()}
        values['prices_usd_per_mtok'] = {
            key: price.model_dump() if hasattr(price, 'model_dump') else price
            for key, price in prices.items()}
        try:
            settings.save_assistant(values)
        except ValueError as exc:
            st.error(f'Not saved: {exc}')
        else:
            st.success('Task models saved. New calls use these assignments; '
                       'stored results stay intact.')
