"""Unit tests for the WhatsApp ChannelAdapter (issue #306, B7).

The Twilio signature verifier is the highest-risk piece (URL + alphabetically-
sorted form params, HMAC-SHA1, base64 — easy to get subtly wrong, issue §9), so
it's pinned to an independently-computed vector and exercised for tamper/replay.
Also covers: Meta X-Hub-Signature-256 verify + the GET verify handshake, inbound
parse for both providers (form + JSON, media, tapped quick-reply, status
callbacks), choice → buttons/list/numbered-text rendering with the WhatsApp
limits, ≤4096 chunking, and the Meta interactive/template payload shapes.

Run:  python3 -m unittest tests.gateway_whatsapp_test   (from charts/workspace/)
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gateway as gw  # noqa: E402
import gateway_review as gr  # noqa: E402
from adapters import whatsapp as wa  # noqa: E402


# ───────────────────────────────────────────────────────────────────────────
# Twilio signature (the risky one)
# ───────────────────────────────────────────────────────────────────────────
class TwilioSignatureTest(unittest.TestCase):
    URL = 'https://ws.example.com/api/gateway/whatsapp/webhook'
    TOKEN = 'test-auth-token-123'

    def _expected(self, url, params):
        data = url + ''.join(f'{k}{params[k]}' for k in sorted(params))
        return base64.b64encode(
            hmac.new(self.TOKEN.encode(), data.encode('utf-8'),
                     hashlib.sha1).digest()).decode()

    def test_matches_reference_vector(self):
        params = {'From': 'whatsapp:+15550001111', 'Body': 'hi', 'MessageSid': 'SM1'}
        sig = wa.TwilioProvider.signature(self.TOKEN, self.URL, params)
        self.assertEqual(sig, self._expected(self.URL, params))

    def test_param_order_independent(self):
        # The signature sorts params, so insertion order must not matter.
        a = wa.TwilioProvider.signature(
            self.TOKEN, self.URL, {'B': '2', 'A': '1', 'C': '3'})
        b = wa.TwilioProvider.signature(
            self.TOKEN, self.URL, {'C': '3', 'A': '1', 'B': '2'})
        self.assertEqual(a, b)

    def test_verify_accepts_good_signature(self):
        prov = wa.TwilioProvider(auth_token=self.TOKEN, account_sid='AC', from_number='')
        form = {'From': 'whatsapp:+15550001111', 'Body': 'hi', 'MessageSid': 'SM1'}
        sig = self._expected(self.URL, form)
        raw = gw.RawRequest(url=self.URL, headers={'X-Twilio-Signature': sig}, form=form)
        self.assertTrue(prov.verify(raw))

    def test_verify_rejects_tampered_body(self):
        prov = wa.TwilioProvider(auth_token=self.TOKEN, account_sid='AC')
        form = {'From': 'whatsapp:+15550001111', 'Body': 'hi', 'MessageSid': 'SM1'}
        sig = self._expected(self.URL, form)
        tampered = dict(form, Body='send money')
        raw = gw.RawRequest(url=self.URL, headers={'X-Twilio-Signature': sig},
                            form=tampered)
        self.assertFalse(prov.verify(raw))

    def test_verify_rejects_wrong_url(self):
        prov = wa.TwilioProvider(auth_token=self.TOKEN, account_sid='AC')
        form = {'From': 'whatsapp:+1', 'Body': 'hi'}
        sig = self._expected(self.URL, form)
        raw = gw.RawRequest(url='https://evil.example.com/hook',
                            headers={'X-Twilio-Signature': sig}, form=form)
        self.assertFalse(prov.verify(raw))

    def test_verify_rejects_missing_header(self):
        prov = wa.TwilioProvider(auth_token=self.TOKEN, account_sid='AC')
        raw = gw.RawRequest(url=self.URL, headers={}, form={'Body': 'hi'})
        self.assertFalse(prov.verify(raw))

    def test_no_token_fails_closed(self):
        prov = wa.TwilioProvider(auth_token='', account_sid='AC')
        raw = gw.RawRequest(url=self.URL, headers={}, form={'Body': 'hi'})
        os.environ.pop('KC_ALLOW_UNSIGNED_WEBHOOKS', None)
        self.assertFalse(prov.verify(raw))

    def test_no_token_allows_when_opted_in(self):
        prov = wa.TwilioProvider(auth_token='', account_sid='AC')
        raw = gw.RawRequest(url=self.URL, headers={}, form={'Body': 'hi'})
        os.environ['KC_ALLOW_UNSIGNED_WEBHOOKS'] = '1'
        try:
            self.assertTrue(prov.verify(raw))
        finally:
            os.environ.pop('KC_ALLOW_UNSIGNED_WEBHOOKS', None)


class TwilioInboundParseTest(unittest.TestCase):
    def _prov(self):
        return wa.TwilioProvider(auth_token='t', account_sid='AC')

    def test_basic_text(self):
        raw = gw.RawRequest(form={'From': 'whatsapp:+15550001111', 'Body': 'hello',
                                  'MessageSid': 'SM42'})
        msg = self._prov().parse_inbound(raw)
        self.assertEqual(msg.channel_identity, 'whatsapp:+15550001111')
        self.assertEqual(msg.text, 'hello')
        self.assertEqual(msg.provider_msg_id, 'SM42')

    def test_media(self):
        raw = gw.RawRequest(form={
            'From': 'whatsapp:+1', 'Body': '', 'MessageSid': 'SM1',
            'NumMedia': '1', 'MediaUrl0': 'https://x/y.jpg',
            'MediaContentType0': 'image/jpeg'})
        msg = self._prov().parse_inbound(raw)
        self.assertEqual(len(msg.media), 1)
        self.assertEqual(msg.media[0].url, 'https://x/y.jpg')

    def test_button_reply(self):
        raw = gw.RawRequest(form={'From': 'whatsapp:+1', 'Body': 'Yes',
                                  'ButtonText': 'Yes', 'MessageSid': 'SM1'})
        msg = self._prov().parse_inbound(raw)
        self.assertEqual(msg.button_reply, 'Yes')

    def test_missing_from_is_none(self):
        self.assertIsNone(self._prov().parse_inbound(gw.RawRequest(form={'Body': 'x'})))

    def test_bare_number_gets_whatsapp_prefix(self):
        raw = gw.RawRequest(form={'From': '+15550001111', 'Body': 'hi'})
        msg = self._prov().parse_inbound(raw)
        self.assertEqual(msg.channel_identity, 'whatsapp:+15550001111')


class TwilioOutboundTest(unittest.TestCase):
    def test_chunks_long_body(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC', from_number='whatsapp:+1')
        caps = gw.Capabilities(max_text_len=100)
        msg = gw.OutboundMessage(channel_identity='whatsapp:+2', text='Z' * 250)
        payloads = prov.build_payloads(msg, caps)
        self.assertEqual(len(payloads), 3)
        self.assertTrue(all(len(p['Body']) <= 100 for p in payloads))
        self.assertEqual(payloads[0]['To'], 'whatsapp:+2')
        self.assertEqual(payloads[0]['From'], 'whatsapp:+1')

    def test_bare_e164_sender_gets_whatsapp_prefix(self):
        # The Settings UI accepts a plain +E164 sender; Twilio's WhatsApp API
        # requires the `whatsapp:` channel prefix on BOTH ends or it rejects the
        # send (21910). Normalize so a naturally-entered number still delivers.
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC',
                                 from_number='+14155238886')
        caps = gw.Capabilities(max_text_len=4096)
        msg = gw.OutboundMessage(channel_identity='whatsapp:+2', text='hi')
        payloads = prov.build_payloads(msg, caps)
        self.assertEqual(payloads[0]['From'], 'whatsapp:+14155238886')
        self.assertEqual(payloads[0]['To'], 'whatsapp:+2')

    def test_already_prefixed_sender_is_untouched(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC',
                                 from_number='whatsapp:+14155238886')
        caps = gw.Capabilities(max_text_len=4096)
        msg = gw.OutboundMessage(channel_identity='whatsapp:+2', text='hi')
        payloads = prov.build_payloads(msg, caps)
        self.assertEqual(payloads[0]['From'], 'whatsapp:+14155238886')

    def test_choice_degrades_to_numbered_text(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC')
        caps = gw.Capabilities(buttons=True, max_buttons=3, max_text_len=4096)
        msg = gw.OutboundMessage(channel_identity='whatsapp:+2', text='Pick:',
                                 quick_replies=['Yes', 'No'])
        payloads = prov.build_payloads(msg, caps)
        self.assertEqual(len(payloads), 1)
        self.assertIn('1. Yes', payloads[0]['Body'])
        self.assertIn('2. No', payloads[0]['Body'])


# ───────────────────────────────────────────────────────────────────────────
# Meta Cloud API
# ───────────────────────────────────────────────────────────────────────────
class MetaSignatureTest(unittest.TestCase):
    SECRET = 'app-secret-xyz'

    def test_verify_matches_raw_body_hmac(self):
        prov = wa.MetaProvider(app_secret=self.SECRET)
        body = b'{"entry":[]}'
        sig = 'sha256=' + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        raw = gw.RawRequest(raw_body=body, headers={'X-Hub-Signature-256': sig})
        self.assertTrue(prov.verify(raw))

    def test_verify_rejects_tampered(self):
        prov = wa.MetaProvider(app_secret=self.SECRET)
        body = b'{"entry":[]}'
        sig = 'sha256=' + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        raw = gw.RawRequest(raw_body=b'{"entry":[1]}',
                            headers={'X-Hub-Signature-256': sig})
        self.assertFalse(prov.verify(raw))


class SignatureFailClosedTest(unittest.TestCase):
    """Both providers must reject an unsigned/forged webhook when the dev
    escape hatch is off — the posture every production render ships (#331)."""

    def setUp(self):
        self._saved = os.environ.pop('KC_ALLOW_UNSIGNED_WEBHOOKS', None)

    def tearDown(self):
        if self._saved is not None:
            os.environ['KC_ALLOW_UNSIGNED_WEBHOOKS'] = self._saved
        else:
            os.environ.pop('KC_ALLOW_UNSIGNED_WEBHOOKS', None)

    # -- Twilio --
    def test_twilio_unsigned_rejected(self):
        prov = wa.TwilioProvider(auth_token='tok', account_sid='AC1')
        self.assertFalse(prov.verify(gw.RawRequest(url='u', headers={}, form={'Body': 'x'})))

    def test_twilio_forged_rejected(self):
        prov = wa.TwilioProvider(auth_token='tok', account_sid='AC1')
        raw = gw.RawRequest(url='u', headers={'X-Twilio-Signature': 'forged'},
                            form={'Body': 'x'})
        self.assertFalse(prov.verify(raw))

    # -- Meta (the missing-signature case was previously unpinned) --
    def test_meta_unsigned_rejected(self):
        prov = wa.MetaProvider(app_secret='s')
        self.assertFalse(prov.verify(gw.RawRequest(raw_body=b'{}', headers={})))

    def test_meta_forged_rejected(self):
        prov = wa.MetaProvider(app_secret='s')
        raw = gw.RawRequest(raw_body=b'{}',
                            headers={'X-Hub-Signature-256': 'sha256=deadbeef'})
        self.assertFalse(prov.verify(raw))

    def test_meta_correct_signature_accepted(self):
        secret, body = 'app-secret', b'{"entry":[]}'
        sig = 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        prov = wa.MetaProvider(app_secret=secret)
        raw = gw.RawRequest(raw_body=body, headers={'X-Hub-Signature-256': sig})
        self.assertTrue(prov.verify(raw))

    def test_opt_in_allows_unsigned_for_dev(self):
        os.environ['KC_ALLOW_UNSIGNED_WEBHOOKS'] = '1'
        # Only when the provider has no secret configured at all (sandbox).
        self.assertTrue(wa.TwilioProvider().verify(
            gw.RawRequest(url='u', headers={}, form={})))
        self.assertTrue(wa.MetaProvider().verify(
            gw.RawRequest(raw_body=b'{}', headers={})))


class MetaHandshakeTest(unittest.TestCase):
    def test_echoes_challenge_on_match(self):
        prov = wa.MetaProvider(verify_token='vt')
        raw = gw.RawRequest(method='GET', query={
            'hub.mode': 'subscribe', 'hub.verify_token': 'vt',
            'hub.challenge': '12345'})
        self.assertEqual(prov.handshake(raw), '12345')

    def test_rejects_wrong_token(self):
        prov = wa.MetaProvider(verify_token='vt')
        raw = gw.RawRequest(method='GET', query={
            'hub.mode': 'subscribe', 'hub.verify_token': 'WRONG',
            'hub.challenge': '12345'})
        self.assertIsNone(prov.handshake(raw))

    def test_empty_configured_token_never_matches(self):
        prov = wa.MetaProvider(verify_token='')
        raw = gw.RawRequest(method='GET', query={
            'hub.mode': 'subscribe', 'hub.verify_token': '', 'hub.challenge': 'x'})
        self.assertIsNone(prov.handshake(raw))


class MetaInboundParseTest(unittest.TestCase):
    def _prov(self):
        return wa.MetaProvider(app_secret='s')

    def _wrap(self, message):
        return gw.RawRequest(raw_body=json.dumps({'entry': [{'changes': [
            {'value': {'messages': [message]}}]}]}).encode())

    def test_text(self):
        msg = self._prov().parse_inbound(self._wrap({
            'from': '15550001111', 'id': 'wamid.1', 'type': 'text',
            'text': {'body': 'hello'}}))
        self.assertEqual(msg.channel_identity, 'whatsapp:15550001111')
        self.assertEqual(msg.text, 'hello')
        self.assertEqual(msg.provider_msg_id, 'wamid.1')

    def test_interactive_button_reply(self):
        msg = self._prov().parse_inbound(self._wrap({
            'from': '1', 'id': 'wamid.2', 'type': 'interactive',
            'interactive': {'type': 'button_reply',
                            'button_reply': {'id': '1', 'title': 'Yes'}}}))
        self.assertEqual(msg.button_reply, 'Yes')

    def test_status_callback_has_no_message(self):
        raw = gw.RawRequest(raw_body=json.dumps({'entry': [{'changes': [
            {'value': {'statuses': [{'status': 'delivered'}]}}]}]}).encode())
        self.assertIsNone(self._prov().parse_inbound(raw))

    def test_media_message(self):
        msg = self._prov().parse_inbound(self._wrap({
            'from': '1', 'id': 'wamid.3', 'type': 'image',
            'image': {'id': 'media-id', 'mime_type': 'image/jpeg'}}))
        self.assertEqual(len(msg.media), 1)
        self.assertEqual(msg.media[0].kind, 'image')


class MetaOutboundTest(unittest.TestCase):
    def setUp(self):
        self.prov = wa.MetaProvider(app_secret='s')
        self.caps = gw.Capabilities(buttons=True, max_buttons=3, max_list_rows=10,
                                    media=True, max_text_len=4096)

    def test_plain_text_payload(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1', text='hi')
        p = self.prov.build_payloads(msg, self.caps)[0]
        self.assertEqual(p['type'], 'text')
        self.assertEqual(p['to'], '+1')
        self.assertEqual(p['text']['body'], 'hi')

    def test_buttons_payload(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1', text='Pick:',
                                 quick_replies=['Yes', 'No'])
        p = self.prov.build_payloads(msg, self.caps)[-1]
        self.assertEqual(p['type'], 'interactive')
        self.assertEqual(p['interactive']['type'], 'button')
        titles = [b['reply']['title']
                  for b in p['interactive']['action']['buttons']]
        self.assertEqual(titles, ['Yes', 'No'])

    def test_list_payload_for_four_to_ten(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1', text='Pick:',
                                 quick_replies=[f'opt{i}' for i in range(5)])
        p = self.prov.build_payloads(msg, self.caps)[-1]
        self.assertEqual(p['interactive']['type'], 'list')
        self.assertEqual(len(p['interactive']['action']['sections'][0]['rows']), 5)

    def test_template_payload(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1', text='',
                                 template='task_complete')
        p = self.prov.build_template_payload(msg)
        self.assertEqual(p['type'], 'template')
        self.assertEqual(p['template']['name'], 'task_complete')


# ───────────────────────────────────────────────────────────────────────────
# choice → interactive rendering (pure, provider-independent)
# ───────────────────────────────────────────────────────────────────────────
class RenderChoiceTest(unittest.TestCase):
    def setUp(self):
        self.caps = gw.Capabilities(buttons=True, max_buttons=3, max_list_rows=10)

    def test_three_or_fewer_are_buttons(self):
        spec = wa.render_choice(['a', 'b', 'c'], self.caps)
        self.assertEqual(spec.kind, 'buttons')
        self.assertEqual([b['id'] for b in spec.buttons], ['1', '2', '3'])

    def test_four_to_ten_are_list(self):
        spec = wa.render_choice([f'o{i}' for i in range(4)], self.caps)
        self.assertEqual(spec.kind, 'list')
        self.assertEqual(len(spec.rows), 4)

    def test_over_ten_is_numbered_text(self):
        spec = wa.render_choice([f'o{i}' for i in range(12)], self.caps)
        self.assertEqual(spec.kind, 'text')
        self.assertIn('1. o0', spec.numbered_text)
        self.assertIn('12. o11', spec.numbered_text)

    def test_long_button_label_truncated(self):
        spec = wa.render_choice(['x' * 40], self.caps)
        self.assertLessEqual(len(spec.buttons[0]['title']), wa.BUTTON_TITLE_MAX)

    def test_long_list_row_keeps_full_text_in_description(self):
        label = 'y' * 40
        spec = wa.render_choice(['a', 'b', 'c', 'd', label], self.caps)
        row = spec.rows[-1]
        self.assertLessEqual(len(row['title']), wa.ROW_TITLE_MAX)
        self.assertEqual(row['description'], label)

    def test_empty_options(self):
        self.assertEqual(wa.render_choice([], self.caps).kind, 'text')


class AdapterWiringTest(unittest.TestCase):
    def test_default_provider_is_twilio_and_not_proactive(self):
        os.environ.pop('KC_WHATSAPP_PROVIDER', None)
        a = wa.WhatsAppAdapter()
        self.assertIsInstance(a.provider, wa.TwilioProvider)
        self.assertFalse(a.capabilities.proactive)  # sandbox can't send templates

    def test_meta_provider_is_proactive(self):
        a = wa.WhatsAppAdapter(provider=wa.MetaProvider(app_secret='s'))
        self.assertTrue(a.capabilities.proactive)

    def test_capabilities_match_whatsapp_limits(self):
        a = wa.WhatsAppAdapter(provider=wa.MetaProvider())
        self.assertTrue(a.capabilities.buttons)
        self.assertEqual(a.capabilities.max_buttons, 3)
        self.assertEqual(a.capabilities.max_list_rows, 10)
        self.assertEqual(a.capabilities.max_text_len, 4096)


# ───────────────────────────────────────────────────────────────────────────
# Provider registry + declarative spec (issue #328)
# ───────────────────────────────────────────────────────────────────────────
class ProviderRegistryTest(unittest.TestCase):
    def test_list_providers_has_twilio_and_meta(self):
        ids = [s.id for s in wa.list_providers()]
        self.assertIn('twilio', ids)
        self.assertIn('meta', ids)

    def test_build_twilio_from_creds(self):
        prov = wa.build_provider('twilio', {
            'account_sid': 'AC123', 'auth_token': 'tok', 'from_number': 'whatsapp:+1'})
        self.assertIsInstance(prov, wa.TwilioProvider)
        self.assertEqual(prov.account_sid, 'AC123')
        self.assertEqual(prov.auth_token, 'tok')
        self.assertEqual(prov.from_number, 'whatsapp:+1')

    def test_build_meta_from_creds(self):
        prov = wa.build_provider('meta', {
            'app_secret': 'sec', 'verify_token': 'vt',
            'phone_number_id': 'PN1', 'access_token': 'at'})
        self.assertIsInstance(prov, wa.MetaProvider)
        self.assertEqual(prov.app_secret, 'sec')
        self.assertEqual(prov.verify_token, 'vt')
        self.assertEqual(prov.phone_number_id, 'PN1')
        self.assertEqual(prov.access_token, 'at')

    def test_build_is_case_insensitive(self):
        self.assertIsInstance(wa.build_provider('META', {}), wa.MetaProvider)
        self.assertIsInstance(wa.build_provider('Twilio', {}), wa.TwilioProvider)

    def test_build_unknown_raises(self):
        with self.assertRaises(ValueError):
            wa.build_provider('nope', {})

    def test_build_with_none_creds_is_empty(self):
        prov = wa.build_provider('twilio', None)
        self.assertEqual(prov.account_sid, '')
        self.assertEqual(prov.auth_token, '')

    def test_build_with_empty_creds_is_empty(self):
        prov = wa.build_provider('meta', {})
        self.assertEqual(prov.app_secret, '')
        self.assertEqual(prov.phone_number_id, '')

    def test_get_provider_spec(self):
        self.assertEqual(wa.get_provider_spec('twilio').id, 'twilio')
        self.assertIsNone(wa.get_provider_spec('nope'))


class ProviderSpecSerializationTest(unittest.TestCase):
    def test_specs_are_json_serializable(self):
        for spec in wa.list_providers():
            # Must not raise — the stage-3 Settings form is driven by this.
            json.dumps(spec.to_dict())

    def test_serialization_round_trips(self):
        for spec in wa.list_providers():
            d = spec.to_dict()
            self.assertEqual(json.loads(json.dumps(d)), d)

    def test_spec_shape_is_complete(self):
        d = wa.get_provider_spec('twilio').to_dict()
        self.assertEqual(d['id'], 'twilio')
        self.assertTrue(d['display_name'])
        self.assertTrue(d['credential_fields'])
        for f in d['credential_fields']:
            self.assertIn('key', f)
            self.assertIn('label', f)
            self.assertIn('secret', f)
        self.assertIn('key', d['sender_field'])
        # Capabilities serialize as their dataclass fields.
        self.assertIn('proactive', d['capabilities'])
        self.assertIn('max_text_len', d['capabilities'])

    def test_secret_flags_are_correct(self):
        tw = {f.key: f for f in wa.get_provider_spec('twilio').credential_fields}
        self.assertTrue(tw['auth_token'].secret)
        self.assertFalse(tw['account_sid'].secret)
        mt = {f.key: f for f in wa.get_provider_spec('meta').credential_fields}
        self.assertTrue(mt['app_secret'].secret)
        self.assertTrue(mt['access_token'].secret)
        self.assertFalse(mt['verify_token'].secret)
        # Sender fields are non-secret identifiers, not credentials.
        self.assertFalse(wa.get_provider_spec('meta').sender_field.secret)

    def test_field_keys_cover_factory_inputs(self):
        # Every key the factory reads must be declared on the spec, so the
        # stage-2 store and stage-3 form know the full set.
        self.assertEqual(
            set(wa.get_provider_spec('twilio').field_keys()),
            {'account_sid', 'auth_token', 'from_number'})
        self.assertEqual(
            set(wa.get_provider_spec('meta').field_keys()),
            {'access_token', 'app_secret', 'verify_token', 'phone_number_id'})


class ProviderCapabilitiesTest(unittest.TestCase):
    def test_capabilities_declared_per_spec(self):
        self.assertFalse(wa.get_provider_spec('twilio').capabilities.proactive)
        self.assertTrue(wa.get_provider_spec('meta').capabilities.proactive)

    def test_whatsapp_limits_shared_across_providers(self):
        for pid in ('twilio', 'meta'):
            caps = wa.get_provider_spec(pid).capabilities
            self.assertTrue(caps.buttons)
            self.assertEqual(caps.max_buttons, 3)
            self.assertEqual(caps.max_list_rows, 10)
            self.assertEqual(caps.max_text_len, 4096)

    def test_adapter_reads_provider_capabilities(self):
        self.assertIs(
            wa.WhatsAppAdapter(provider=wa.MetaProvider()).capabilities,
            wa.MetaProvider.capabilities)


class ProviderFromEnvTest(unittest.TestCase):
    """The env path stays valid for platform-managed deploys and must behave
    exactly as before #328."""

    _VARS = ('KC_WHATSAPP_PROVIDER', 'KC_META_APP_SECRET', 'KC_META_VERIFY_TOKEN',
             'KC_META_PHONE_NUMBER_ID', 'KC_META_ACCESS_TOKEN',
             'KC_TWILIO_AUTH_TOKEN', 'KC_TWILIO_ACCOUNT_SID', 'KC_TWILIO_FROM')

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self._VARS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_is_twilio(self):
        self.assertIsInstance(wa._provider_from_env(), wa.TwilioProvider)

    def test_meta_selected(self):
        os.environ['KC_WHATSAPP_PROVIDER'] = 'meta'
        self.assertIsInstance(wa._provider_from_env(), wa.MetaProvider)

    def test_unknown_value_falls_back_to_twilio(self):
        os.environ['KC_WHATSAPP_PROVIDER'] = 'bogus'
        self.assertIsInstance(wa._provider_from_env(), wa.TwilioProvider)

    def test_reads_twilio_credentials(self):
        os.environ['KC_TWILIO_ACCOUNT_SID'] = 'AC9'
        os.environ['KC_TWILIO_AUTH_TOKEN'] = 'tk9'
        os.environ['KC_TWILIO_FROM'] = 'whatsapp:+19'
        prov = wa._provider_from_env()
        self.assertEqual(prov.account_sid, 'AC9')
        self.assertEqual(prov.auth_token, 'tk9')
        self.assertEqual(prov.from_number, 'whatsapp:+19')

    def test_reads_meta_credentials(self):
        os.environ['KC_WHATSAPP_PROVIDER'] = 'meta'
        os.environ['KC_META_APP_SECRET'] = 's9'
        os.environ['KC_META_PHONE_NUMBER_ID'] = 'PN9'
        prov = wa._provider_from_env()
        self.assertEqual(prov.app_secret, 's9')
        self.assertEqual(prov.phone_number_id, 'PN9')


# ───────────────────────────────────────────────────────────────────────────
# Sender normalization (issue #458)
# ───────────────────────────────────────────────────────────────────────────
class NormalizeWaSenderTest(unittest.TestCase):
    """A malformed sender used to save fine and then 400 every outbound send
    (the real `(478) 347-7453` incident, issue #458). normalize_wa_sender is
    the single source of truth: coerce fixable formatting to whatsapp:+E164,
    reject the unfixable with a human-readable reason."""

    def test_display_formatting_is_stripped(self):
        norm, err = wa.normalize_wa_sender('+1 (478) 347-7453')
        self.assertIsNone(err)
        self.assertEqual(norm, 'whatsapp:+14783477453')

    def test_bare_e164_gets_prefixed(self):
        self.assertEqual(wa.normalize_wa_sender('+14155238886'),
                         ('whatsapp:+14155238886', None))

    def test_canonical_value_is_unchanged(self):
        self.assertEqual(wa.normalize_wa_sender('whatsapp:+14155238886'),
                         ('whatsapp:+14155238886', None))

    def test_prefix_is_case_insensitive_and_formatting_inside_ok(self):
        self.assertEqual(wa.normalize_wa_sender('WhatsApp:+1 415-523-8886'),
                         ('whatsapp:+14155238886', None))

    def test_number_without_country_code_is_rejected(self):
        # The incident value: no '+', so the country code is unknowable —
        # reject loudly instead of storing a sender that 400s every send.
        norm, err = wa.normalize_wa_sender('(478) 347-7453')
        self.assertIsNone(norm)
        self.assertIn('+', err)

    def test_prefixed_but_malformed_is_rejected(self):
        norm, err = wa.normalize_wa_sender('whatsapp:(478) 347-7453')
        self.assertIsNone(norm)
        self.assertTrue(err)

    def test_not_a_number_is_rejected(self):
        for bad in ('+1-800-FLOWERS', 'sandbox', '+0123456789', '+1234',
                    '+' + '9' * 20):
            norm, err = wa.normalize_wa_sender(bad)
            self.assertIsNone(norm, bad)
            self.assertTrue(err, bad)

    def test_blank_is_absent_not_invalid(self):
        self.assertEqual(wa.normalize_wa_sender(''), ('', None))
        self.assertEqual(wa.normalize_wa_sender('   '), ('', None))
        self.assertEqual(wa.normalize_wa_sender(None), ('', None))

    def test_registry_exposes_normalizer_by_format(self):
        self.assertIs(wa.get_field_normalizer('wa_sender'), wa.normalize_wa_sender)
        self.assertIsNone(wa.get_field_normalizer(''))
        self.assertIsNone(wa.get_field_normalizer('nope'))

    def test_twilio_sender_declares_format_meta_does_not(self):
        # Meta's phone_number_id is an opaque id, NOT a dialable number — it
        # must never be forced through E.164 normalization.
        self.assertEqual(wa.get_provider_spec('twilio').sender_field.format,
                         'wa_sender')
        self.assertEqual(wa.get_provider_spec('meta').sender_field.format, '')
        # Serialized so the Settings form can mirror the check inline.
        d = wa.get_provider_spec('twilio').to_dict()
        self.assertEqual(d['sender_field']['format'], 'wa_sender')


class WaAddrNormalizationTest(unittest.TestCase):
    """_wa_addr routes through normalize_wa_sender so a formatted-but-fixable
    sender stored before save-time validation existed still delivers."""

    def test_formatted_sender_is_cleaned_in_payload(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC',
                                 from_number='+1 (415) 523-8886')
        caps = gw.Capabilities(max_text_len=4096)
        msg = gw.OutboundMessage(channel_identity='whatsapp:+15550001111', text='hi')
        payloads = prov.build_payloads(msg, caps)
        self.assertEqual(payloads[0]['From'], 'whatsapp:+14155238886')

    def test_unfixable_value_falls_back_to_legacy_behavior(self):
        # Not silently dropped — Twilio still rejects it, but validate() and the
        # save path now catch this class of value before it gets here.
        self.assertEqual(wa.TwilioProvider._wa_addr('(478) 347-7453'),
                         '(478) 347-7453')

    def test_empty_stays_empty(self):
        self.assertEqual(wa.TwilioProvider._wa_addr(''), '')


class TwilioValidateSenderTest(unittest.TestCase):
    """Test-connection must fail on an implausible sender (issue #458): the
    account probe alone stayed green while every send 400'd."""

    def test_invalid_sender_fails_without_network(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC1',
                                 from_number='(478) 347-7453')
        with mock.patch('urllib.request.urlopen') as m:
            ok, detail = prov.validate()
        self.assertFalse(ok)
        self.assertIn('sender number', detail)
        m.assert_not_called()

    def test_valid_sender_proceeds_to_account_probe(self):
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC1',
                                 from_number='whatsapp:+14155238886')
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=200)
        with mock.patch('urllib.request.urlopen', return_value=resp) as m:
            ok, detail = prov.validate()
        self.assertTrue(ok)
        m.assert_called_once()

    def test_empty_sender_still_validates_creds_only(self):
        # Env-managed deploys may configure the sender elsewhere — an absent
        # sender is not an invalid one.
        prov = wa.TwilioProvider(auth_token='t', account_sid='AC1')
        resp = mock.MagicMock()
        resp.__enter__.return_value = mock.Mock(status=200)
        with mock.patch('urllib.request.urlopen', return_value=resp):
            ok, _detail = prov.validate()
        self.assertTrue(ok)


class ProviderErrorHintTest(unittest.TestCase):
    """Outbound failures must be self-diagnosable from the runner log (issue
    #458): surface the provider's numeric error code + short message, never
    just 'HTTP Error 400'."""

    @staticmethod
    def _http_error(code, body):
        import io
        return urllib.error.HTTPError(
            'https://api.twilio.com/x', code, 'Bad Request', {},
            io.BytesIO(body))

    def test_twilio_error_code_surfaces(self):
        e = self._http_error(400, json.dumps({
            'code': 21910, 'message': "Invalid 'From' and 'To' pair",
            'status': 400}).encode())
        with mock.patch('urllib.request.urlopen', side_effect=e):
            result = wa._do_send(mock.Mock(), id_key='sid')
        self.assertFalse(result.ok)
        self.assertIn('21910', result.error)
        self.assertIn('HTTP 400', result.error)
        self.assertEqual(result.status, 400)

    def test_meta_nested_error_code_surfaces(self):
        e = self._http_error(400, json.dumps({
            'error': {'code': 131030, 'message': 'Recipient not in allowed list'},
        }).encode())
        with mock.patch('urllib.request.urlopen', side_effect=e):
            result = wa._do_send(mock.Mock(), id_key='messages')
        self.assertIn('131030', result.error)

    def test_unparseable_body_still_reports_status(self):
        e = self._http_error(500, b'<html>oops</html>')
        with mock.patch('urllib.request.urlopen', side_effect=e):
            result = wa._do_send(mock.Mock(), id_key='sid')
        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'HTTP 500')

    def test_non_http_error_keeps_legacy_shape(self):
        with mock.patch('urllib.request.urlopen', side_effect=OSError('boom')):
            result = wa._do_send(mock.Mock(), id_key='sid')
        self.assertFalse(result.ok)
        self.assertIn('OSError', result.error)


# ───────────────────────────────────────────────────────────────────────────
# Board review cards (#709 Part 4) — adapter wire formats
# ───────────────────────────────────────────────────────────────────────────
class ReviewButtonWireTest(unittest.TestCase):
    BUTTONS = [{'id': 'kcr1.a.x', 'title': 'Approve'},
               {'id': 'kcr1.r.x', 'title': 'Reject'},
               {'id': 'kcr1.o.x', 'title': 'Open in app'}]

    def test_twilio_tap_carries_ButtonPayload(self):
        raw = gw.RawRequest(form={'From': 'whatsapp:+1555', 'Body': 'Approve',
                                  'ButtonText': 'Approve',
                                  'ButtonPayload': 'kcr1.a.x', 'MessageSid': 'SM1'})
        inbound = wa.TwilioProvider().parse_inbound(raw)
        self.assertEqual(inbound.button_payload, 'kcr1.a.x')
        self.assertEqual(inbound.button_reply, 'Approve')

    def test_meta_interactive_tap_carries_the_button_id(self):
        body = {'entry': [{'changes': [{'value': {'messages': [{
            'from': '1555', 'id': 'wamid.1', 'type': 'interactive',
            'interactive': {'type': 'button_reply', 'button_reply': {
                'id': 'kcr1.a.x', 'title': 'Approve'}}}]}}]}]}
        inbound = wa.MetaProvider().parse_inbound(
            gw.RawRequest(raw_body=json.dumps(body).encode()))
        self.assertEqual(inbound.button_payload, 'kcr1.a.x')
        self.assertEqual(inbound.button_reply, 'Approve')   # unchanged for chat

    def test_meta_template_tap_carries_the_payload(self):
        body = {'entry': [{'changes': [{'value': {'messages': [{
            'from': '1555', 'id': 'wamid.2', 'type': 'button',
            'button': {'payload': 'kcr1.r.x', 'text': 'Reject'}}]}}]}]}
        inbound = wa.MetaProvider().parse_inbound(
            gw.RawRequest(raw_body=json.dumps(body).encode()))
        self.assertEqual(inbound.button_payload, 'kcr1.r.x')

    def test_meta_renders_explicit_ids_not_positions(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1555', text='hi',
                                 buttons=self.BUTTONS,
                                 quick_replies=[b['title'] for b in self.BUTTONS])
        p = wa.MetaProvider().build_payloads(msg, wa.META_CAPS)[-1]
        ids = [b['reply']['id'] for b in p['interactive']['action']['buttons']]
        self.assertEqual(ids, ['kcr1.a.x', 'kcr1.r.x', 'kcr1.o.x'])

    def test_twilio_sandbox_falls_back_to_numbered_text(self):
        msg = gw.OutboundMessage(channel_identity='whatsapp:+1555', text='hi',
                                 buttons=self.BUTTONS,
                                 quick_replies=[b['title'] for b in self.BUTTONS])
        body = wa.TwilioProvider().build_payloads(msg, wa.TWILIO_CAPS)[0]['Body']
        self.assertIn('1. Approve\n2. Reject\n3. Open in app', body)
        self.assertNotIn('kcr1', body)

    def test_meta_template_sets_body_params_and_button_payloads(self):
        msg = gw.OutboundMessage(
            channel_identity='whatsapp:+1555', template='board_review',
            buttons=self.BUTTONS,
            template_args={'provider_name': 'kc_board_review', 'language': 'en_US',
                           'body_params': ['SUP-46', 'comment', 'Hello']})
        p = wa.MetaProvider().build_template_payload(msg)
        t = p['template']
        self.assertEqual(t['name'], 'kc_board_review')
        self.assertEqual(t['language'], {'code': 'en_US'})
        self.assertEqual([x['text'] for x in t['components'][0]['parameters']],
                         ['SUP-46', 'comment', 'Hello'])
        self.assertEqual([(c['index'], c['parameters'][0]['payload'])
                          for c in t['components'][1:]],
                         [('0', 'kcr1.a.x'), ('1', 'kcr1.r.x'), ('2', 'kcr1.o.x')])

    def test_twilio_content_template_sends_sid_and_variables(self):
        msg = gw.OutboundMessage(
            channel_identity='whatsapp:+1555', template='board_review',
            buttons=self.BUTTONS,
            template_args={'content_sid': 'HX123', 'body_params': ['SUP-46', 'c', 'Hi']})
        p = wa.TwilioProvider(from_number='+14155238886').build_payloads(msg, wa.TWILIO_CAPS)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]['ContentSid'], 'HX123')
        self.assertNotIn('Body', p[0])
        self.assertEqual(json.loads(p[0]['ContentVariables']),
                         {'1': 'SUP-46', '2': 'c', '3': 'Hi', '4': 'kcr1.a.x',
                          '5': 'kcr1.r.x', '6': 'kcr1.o.x'})


# ───────────────────────────────────────────────────────────────────────────
# Board review cards — the notifier, against a real gateway core
# ───────────────────────────────────────────────────────────────────────────
PHONE = 'whatsapp:+15550001111'


class FakeWhatsApp(gw.EchoAdapter):
    """EchoAdapter dressed as WhatsApp: a provider name (template selection
    keys on it) and the inbound button payload. Never touches the network."""

    name = 'whatsapp'

    def __init__(self, provider='meta', proactive=True):
        super().__init__(proactive=proactive)
        self.provider = mock.Mock()
        self.provider.name = provider

    def inbound(self, raw):
        frm = raw.form.get('from')
        if not frm:
            return None
        return gw.InboundMessage(
            channel='whatsapp', channel_identity=frm,
            text=raw.form.get('text', ''), provider_msg_id=raw.form.get('id', ''),
            button_reply=raw.form.get('button', ''),
            button_payload=raw.form.get('payload', ''))


def staged_record(*actions, item_id='46', content_hash='c' * 64):
    return {'board_id': 'acme', 'item_id': item_id, 'item_key': f'SUP-{item_id}',
            'item_title': 'Refund not received', 'content_hash': content_hash,
            'state': 'pending',
            'actions': [dict({'id': f'a{i + 1}', 'state': 'pending'}, **a)
                        for i, a in enumerate(actions)]}


def comment(body, **extra):
    return {'action': 'comment', 'params': dict({'body': body}, **extra)}


class ReviewHarness(unittest.TestCase):
    """A real ConversationGateway + IdentityRegistry on a temp dir, a fake
    WhatsApp adapter, and a decider that records what it was asked."""

    PROVIDER = 'meta'
    PROACTIVE = True

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kc-gw-review-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.registry = gw.IdentityRegistry(self.dir)
        self.templates = gw.TemplateRegistry(self.dir)
        self.gw = gw.ConversationGateway(registry=self.registry,
                                         templates=self.templates,
                                         client_factory=lambda b: None)
        self.adapter = FakeWhatsApp(self.PROVIDER, self.PROACTIVE)
        self.records = {}
        self.decisions = []
        self.decide_result = ({'replayed': False, 'result': {'ok': True}}, None)

        def decider(decision, board_id, item_id, **kw):
            self.decisions.append((decision, board_id, item_id, kw))
            return self.decide_result

        self.notifier = gr.ReviewNotifier(
            self.gw, adapter_getter=lambda: self.adapter,
            record_getter=lambda b, i: self.records.get((b, i)),
            decider=decider, spawn=lambda fn: fn())
        self.gw.review_handler = self.notifier.handle_inbound

    def pair(self, phone=PHONE):
        self.registry.bind(phone, 'whatsapp', workspace='w',
                           workspace_host='ws.example.com', token='tok')

    def open_window(self, phone=PHONE):
        self.gw._last_inbound[phone] = time.time()

    def card_for(self, record):
        self.records[('acme', record['item_id'])] = record
        self.adapter.sent.clear()
        return self.notifier.deliver('acme', record['item_id'])

    def tap(self, payload='', text='', phone=PHONE, msg_id=None):
        self._n = getattr(self, '_n', 0) + 1
        return self.gw.handle_inbound(self.adapter, gw.RawRequest(form={
            'from': phone, 'payload': payload, 'text': text,
            'id': msg_id or f'm{self._n}'}))

    def button(self, verb):
        msg = next(m for m in reversed(self.adapter.sent) if m.buttons)
        return next(b['id'] for b in msg.buttons
                    if b['id'].startswith(f'kcr1.{verb}.'))


class ReviewCardShapeTest(ReviewHarness):
    """Short single comment → full text + three buttons; everything else → a
    one-line summary and Open in app only."""

    def setUp(self):
        super().setUp()
        self.pair()
        self.open_window()

    def test_a_short_comment_shows_the_FULL_text_and_three_buttons(self):
        body = 'Hi Dana —\nthe refund was issued on the 3rd.'
        self.assertEqual(self.card_for(staged_record(comment(body))), 1)
        msg = self.adapter.last()
        self.assertIn(body, msg.text)
        self.assertEqual([b['title'] for b in msg.buttons],
                         ['Approve', 'Reject', 'Open in app'])
        self.assertTrue(all(b['id'].startswith('kcr1.') for b in msg.buttons))

    def test_public_flag_is_spelled_out(self):
        self.card_for(staged_record(comment('Hello', public=True)))
        self.assertIn('the customer will see it', self.adapter.last().text)

    def test_a_LONG_comment_only_offers_open_in_app(self):
        body = 'x' * (gr.SHORT_BODY_MAX + 1)
        self.card_for(staged_record(comment(body)))
        msg = self.adapter.last()
        self.assertEqual(msg.buttons, [])
        self.assertNotIn(body, msg.text)
        self.assertIn('https://ws.example.com/board?board=acme&review=46', msg.text)

    def test_SEVERAL_writes_only_offer_open_in_app(self):
        self.card_for(staged_record(
            comment('ok'), {'action': 'set_status', 'params': {'state': 'closed'}}))
        msg = self.adapter.last()
        self.assertEqual(msg.buttons, [])
        self.assertIn('2 proposed writes', msg.text)

    def test_a_status_change_only_offers_open_in_app(self):
        self.card_for(staged_record({'action': 'set_status',
                                     'params': {'state': 'closed'}}))
        self.assertEqual(self.adapter.last().buttons, [])

    def test_a_comment_with_other_params_is_not_short(self):
        self.card_for(staged_record(comment('ok', assignee='bob')))
        self.assertEqual(self.adapter.last().buttons, [])

    def test_a_long_card_cannot_be_approved_even_with_a_valid_signature(self):
        record = staged_record(comment('x' * (gr.SHORT_BODY_MAX + 1)))
        self.card_for(record)
        card = next(iter(self.notifier.store._all()))
        forged = gr.encode_payload(self.notifier.store.key(), 'a', card['nonce'],
                                   card['content_hash'])
        self.tap(payload=forged)
        self.assertEqual(self.decisions, [])

    def test_the_same_proposal_is_sent_once(self):
        record = staged_record(comment('Hello'))
        self.assertEqual(self.card_for(record), 1)
        self.assertEqual(self.card_for(record), 0)

    def test_a_decided_or_empty_record_sends_nothing(self):
        record = staged_record(comment('Hello'))
        record['state'] = 'approved'
        self.assertEqual(self.card_for(record), 0)
        self.assertEqual(self.card_for(staged_record()), 0)

    def test_only_board_review_feed_items_are_picked_up(self):
        review = {'waiting': True, 'source': 'board:acme',
                  'links': [{'ref': 'board:acme:I_kw:46'}]}
        self.assertEqual(gr.review_ref(review), ('acme', 'I_kw:46'))
        self.assertIsNone(gr.review_ref(dict(review, waiting=False)))
        self.assertIsNone(gr.review_ref(dict(review, source='task:t1')))
        self.assertIsNone(gr.review_ref({'waiting': True, 'source': 'board:acme',
                                         'links': [{'ref': 'task:t1'}]}))
        self.assertFalse(self.notifier.on_feed_item({'kind': 'activity'}))


class ReviewTapTest(ReviewHarness):
    def setUp(self):
        super().setUp()
        self.pair()
        self.open_window()
        self.record = staged_record(comment('Hello Dana'))
        self.card_for(self.record)

    def test_approve_goes_to_the_decider_with_the_bound_hash_and_digest(self):
        result = self.tap(payload=self.button('a'))
        self.assertEqual(result.action, 'review_approve')
        (decision, board, item, kw), = self.decisions
        self.assertEqual((decision, board, item), ('approve', 'acme', '46'))
        self.assertEqual(kw['content_hash'], self.record['content_hash'])
        self.assertEqual(kw['proposal_digest'],
                         gr.board_review.proposal_digest(self.record))
        self.assertTrue(kw['approval_id'].startswith('wa-'))
        self.assertIn('Approved', self.adapter.last().text)

    def test_a_double_tap_reuses_the_approval_id_so_the_decider_replays(self):
        self.tap(payload=self.button('a'))
        self.decide_result = ({'replayed': True, 'result': {'ok': True}}, None)
        self.tap(payload=self.button('a'))
        self.assertEqual(self.decisions[0][3]['approval_id'],
                         self.decisions[1][3]['approval_id'])
        self.assertIn('already approved', self.adapter.last().text)

    def test_a_retried_DELIVERY_is_dropped_before_the_decider(self):
        self.tap(payload=self.button('a'), msg_id='wamid.same')
        self.assertEqual(self.tap(payload=self.button('a'),
                                  msg_id='wamid.same').action, 'duplicate')
        self.assertEqual(len(self.decisions), 1)

    def test_reject_uses_its_own_approval_id(self):
        self.tap(payload=self.button('r'))
        self.assertEqual(self.decisions[0][0], 'reject')
        self.assertTrue(self.decisions[0][3]['approval_id'].endswith('-r'))
        self.assertIn('Rejected', self.adapter.last().text)

    def test_a_changed_ticket_gets_the_clear_refusal(self):
        from boards.review import ReviewError
        for code in ('stale', 'hash_mismatch'):
            self.decide_result = (None, ReviewError(code, 'x'))
            self.tap(payload=self.button('a'))
            self.assertIn(gr.CHANGED_TEXT, self.adapter.last().text)
            self.assertIn('https://ws.example.com/board?', self.adapter.last().text)

    def test_open_in_app_replies_with_the_link_and_decides_nothing(self):
        self.tap(payload=self.button('o'))
        self.assertEqual(self.decisions, [])
        self.assertIn('https://ws.example.com/board?board=acme&review=46',
                      self.adapter.last().text)

    def _silent(self, payload, phone=PHONE):
        before = len(self.adapter.sent)
        result = self.tap(payload=payload, phone=phone)
        self.assertEqual(self.decisions, [])
        return result, self.adapter.sent[before:]

    def test_a_tampered_mac_does_nothing(self):
        good = self.button('a')
        result, sent = self._silent(good[:-1] + ('0' if good[-1] != '0' else '1'))
        self.assertEqual(result.action, 'rejected')
        self.assertEqual(sent, [])

    def test_a_verb_swapped_onto_another_mac_does_nothing(self):
        approve = self.button('a').split('.')
        reject = self.button('r').split('.')
        self._silent('.'.join(approve[:4] + reject[4:]))

    def test_a_signed_payload_with_the_WRONG_hash_does_nothing(self):
        nonce = self.button('a').split('.')[2]
        key = self.notifier.store.key()
        result, sent = self._silent(gr.encode_payload(key, 'a', nonce, 'd' * 64))
        self.assertEqual(sent, [])

    def test_a_signed_payload_for_an_unknown_card_does_nothing(self):
        key = self.notifier.store.key()
        self._silent(gr.encode_payload(key, 'a', 'f' * 24,
                                       self.record['content_hash']))

    def test_a_payload_signed_with_another_key_does_nothing(self):
        nonce = self.button('a').split('.')[2]
        self._silent(gr.encode_payload(b'k' * 32, 'a', nonce,
                                       self.record['content_hash']))

    def test_an_UNPAIRED_sender_gets_nothing_actioned(self):
        result, sent = self._silent(self.button('a'), phone='whatsapp:+19998887777')
        self.assertEqual(result.action, 'not_linked')
        self.assertTrue(all('kcr1' not in m.text for m in sent))

    def test_another_PAIRED_number_cannot_use_my_card(self):
        self.pair('whatsapp:+19998887777')
        result, sent = self._silent(self.button('a'), phone='whatsapp:+19998887777')
        self.assertEqual(result.action, 'rejected')
        self.assertEqual(sent, [])

    def test_a_revoked_token_decides_nothing(self):
        self.gw.token_verifier = lambda t: False
        result, _sent = self._silent(self.button('a'))
        self.assertEqual(result.detail, 'token invalid')

    def test_a_decider_that_raises_is_contained(self):
        def boom(*a, **k):
            raise RuntimeError('vendor down')
        self.notifier.decider = boom
        result = self.tap(payload=self.button('a'))
        self.assertEqual(result.action, 'error')


class ReviewWindowAndTemplateTest(ReviewHarness):
    """In the 24-h window → a free-form interactive card; outside it → the
    approved template, or nothing at all."""

    def setUp(self):
        super().setUp()
        self.pair()

    def test_inside_the_window_it_is_free_form(self):
        self.open_window()
        self.card_for(staged_record(comment('Hello')))
        self.assertIsNone(self.adapter.last().template)

    def test_outside_the_window_without_an_approved_template_nothing_is_sent(self):
        # Meta enforces registration: the built-in default is not enough.
        self.assertEqual(self.card_for(staged_record(comment('Hello'))), 0)
        self.assertEqual(self.adapter.sent, [])

    def test_outside_the_window_the_registered_template_carries_text_and_buttons(self):
        self.templates.approve('board_review', provider_name='kc_board_review_v2',
                               language='en_US')
        self.assertEqual(self.card_for(staged_record(
            comment('Line one.\n\nLine   two.'))), 1)
        msg = self.adapter.last()
        self.assertEqual(msg.template, 'board_review')
        self.assertEqual(msg.template_args['provider_name'], 'kc_board_review_v2')
        self.assertEqual(msg.template_args['language'], 'en_US')
        # Template params can't hold newlines: flattened, every word kept.
        self.assertEqual(msg.template_args['body_params'],
                         ['SUP-46', 'comment', 'Line one. Line two.'])
        self.assertEqual(len(msg.buttons), 3)

    def test_a_long_proposal_out_of_window_uses_the_link_template(self):
        self.templates.approve('board_review_link')
        self.card_for(staged_record(comment('x' * (gr.SHORT_BODY_MAX + 1))))
        msg = self.adapter.last()
        self.assertEqual(msg.template, 'board_review_link')
        self.assertEqual(msg.buttons, [])
        self.assertIn('https://ws.example.com/board?',
                      msg.template_args['body_params'][2])

    def test_a_failed_send_records_no_card_so_a_later_emit_retries(self):
        self.open_window()
        self.adapter.outbound = lambda m: gw.DeliveryResult(ok=False, error='HTTP 500')
        record = staged_record(comment('Hello'))
        self.assertEqual(self.card_for(record), 0)
        self.adapter.outbound = gw.EchoAdapter.outbound.__get__(self.adapter)
        self.assertEqual(self.card_for(record), 1)

    def test_a_raising_adapter_never_escapes_the_feed_hook(self):
        self.open_window()
        def boom(_m):
            raise RuntimeError('provider down')
        self.adapter.outbound = boom
        self.records[('acme', '46')] = staged_record(comment('Hello'))
        item = {'waiting': True, 'source': 'board:acme',
                'links': [{'ref': 'board:acme:46'}]}
        self.assertTrue(self.notifier.on_feed_item(item))   # queued, no raise


class TwilioSandboxReviewTest(ReviewHarness):
    """The sandbox has no Content templates: in the window the card is
    numbered text, and a bare digit decides only while that card is still the
    last thing the gateway sent."""

    PROVIDER = 'twilio'
    PROACTIVE = False

    def setUp(self):
        super().setUp()
        self.pair()

    def test_numbered_reply_1_approves_and_2_rejects(self):
        self.open_window()
        self.card_for(staged_record(comment('Hello')))
        self.assertEqual(self.adapter.last().quick_replies,
                         ['Approve', 'Reject', 'Open in app'])
        self.tap(text='1')
        self.assertEqual(self.decisions[-1][0], 'approve')
        self.card_for(staged_record(comment('Other'), item_id='47'))
        self.tap(text='2')
        self.assertEqual(self.decisions[-1][0], 'reject')
        self.assertEqual(self.decisions[-1][2], '47')

    def test_a_digit_after_another_message_is_chat_not_a_decision(self):
        self.open_window()
        self.card_for(staged_record(comment('Hello')))
        self.gw._send(self.adapter, PHONE, 'Pick one:\n1. foo\n2. bar')
        result = self.tap(text='1')
        self.assertEqual(self.decisions, [])
        self.assertNotEqual(result.action, 'review_approve')

    def test_outside_the_window_the_sandbox_sends_nothing(self):
        self.assertEqual(self.card_for(staged_record(comment('Hello'))), 0)

    def test_a_twilio_template_needs_a_content_sid(self):
        self.templates.approve('board_review')            # default name only
        self.assertEqual(self.card_for(staged_record(comment('Hello'))), 0)
        self.templates.approve('board_review', provider_name='HXabc123')
        self.assertEqual(self.card_for(staged_record(comment('Hello'))), 1)
        msg = self.adapter.last()
        self.assertEqual(msg.template_args['content_sid'], 'HXabc123')
        self.assertEqual(len(msg.buttons), 3)


class SealedAddressTest(unittest.TestCase):
    """The address a review card is sent to is kept sealed, never in the
    clear, and only ever opens for the record it was sealed into."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kc-gw-seal-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.reg = gw.IdentityRegistry(self.dir)

    def test_round_trip_and_never_in_the_clear(self):
        self.reg.bind(PHONE, 'whatsapp', workspace='w', workspace_host='h', token='t')
        rec = self.reg.lookup(PHONE)
        self.assertNotIn('15550001111', json.dumps(rec))
        self.assertEqual(self.reg.open_address(rec), PHONE)
        self.assertEqual([r['notify_to'] for r in self.reg.notify_targets('whatsapp')],
                         [PHONE])
        self.assertNotIn('notify_sealed', json.dumps(self.reg.list_links()))

    def test_a_sealed_address_moved_to_another_record_does_not_open(self):
        self.reg.bind(PHONE, 'whatsapp', workspace='w', workspace_host='h', token='t')
        self.reg.bind('whatsapp:+19998887777', 'whatsapp', workspace='w',
                      workspace_host='h', token='t')
        mine = self.reg.lookup(PHONE)
        other = self.reg.lookup('whatsapp:+19998887777')
        self.assertIsNone(self.reg.open_address(
            dict(other, notify_sealed=mine['notify_sealed'])))

    def test_an_old_link_is_backfilled_on_its_next_inbound(self):
        self.reg.bind(PHONE, 'whatsapp', workspace='w', workspace_host='h', token='t')
        rec = self.reg.lookup(PHONE)
        rec.pop('notify_sealed')
        self.reg._atomic_write(self.reg._identity_path(rec['identity_hash']), rec)
        self.assertEqual(self.reg.notify_targets('whatsapp'), [])
        self.reg.remember_address(PHONE)
        self.assertEqual(len(self.reg.notify_targets('whatsapp')), 1)


if __name__ == '__main__':
    unittest.main()
