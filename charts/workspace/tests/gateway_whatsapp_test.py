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


if __name__ == '__main__':
    unittest.main()
