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
import sys
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import gateway as gw  # noqa: E402
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


if __name__ == '__main__':
    unittest.main()
