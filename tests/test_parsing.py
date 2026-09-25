# -*- coding: utf-8 -*-
"""Lectura de XML recibidos e interpretación de códigos del SII.

Ruta real: tests/test_parsing.py
"""
from datetime import date, datetime

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.tf_dte_cl_intercambio.models.account_move import client_state_from_events
from odoo.addons.tf_dte_cl_intercambio.models.dte_received import (
    payment_method_from_xml,
    sii_local_to_utc,
    utc_to_sii_date,
    verify_state_from_code,
)
from odoo.addons.tf_dte_cl_intercambio.models.exchange_envelope import (
    _global_adjustments,
    parse_envelope,
    parse_response,
)
from odoo.addons.tf_dte_cl_intercambio.models.sii_client import normalize_claim_response, parse_reception_date

from .common import COMPANY_RUT, SUPPLIER_RUT, dte_xml, envio_xml, response_xml, wrapped_xml

EMISSION = date(2026, 9, 1)


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestParseEnvelope(TransactionCase):

    def test_standard_envelope(self):
        raw = envio_xml(dte_xml(120, EMISSION, payment='1', lines=((1, 50000, False), (2, 1500, True))))
        data = parse_envelope(raw)
        self.assertTrue(data['has_caratula'])
        self.assertEqual((data['issuer_vat'], data['receiver_vat']), (SUPPLIER_RUT, COMPANY_RUT))
        document = data['documents'][0]
        self.assertEqual((document['document_type'], document['folio'], document['date']),
                         ('33', '120', '2026-09-01'))
        self.assertEqual((document['amount_net'], document['amount_exempt'], document['amount_tax'],
                          document['amount_total']), (50000, 3000, 9500, 62500))
        self.assertEqual([line['is_exempt'] for line in document['lines']], [False, True])

    def test_wrapped_without_caratula(self):
        raw = wrapped_xml(dte_xml(54346541, EMISSION, discount=2201, namespace=True))
        data = parse_envelope(raw)
        self.assertFalse(data['has_caratula'])
        self.assertEqual(data['receiver_vat'], COMPANY_RUT)             # se toma del DTE
        document = data['documents'][0]
        self.assertEqual(document['amount_net'], 6602)                  # 8803 - 2201
        self.assertEqual([line['amount'] for line in document['lines']], [8803, -2201])

    def test_bare_dte(self):
        data = parse_envelope(dte_xml(7, EMISSION, namespace=True).encode())
        self.assertFalse(data['has_caratula'])
        self.assertEqual(data['documents'][0]['folio'], '7')

    def test_rejects_invalid_files(self):
        for raw in (b'roto', b'<Foo/>', b'<EnvioDTE><SetDTE/></EnvioDTE>'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_envelope(raw)

    def test_references(self):
        raw = envio_xml(dte_xml(12, EMISSION, doc_type='61', references=(
            ('33', '700', '2026-08-30', '3', 'Corrige montos'), ('801', 'OC-55', '2026-08-01', '', 'Orden de compra'),
        )))
        references = parse_envelope(raw)['documents'][0]['references']
        self.assertEqual([(r['document_type'], r['folio'], r['code']) for r in references],
                         [('33', '700', '3'), ('801', 'OC-55', '')])

    def test_global_adjustments(self):
        lines = [{'amount': 10000, 'is_exempt': False}, {'amount': 2000, 'is_exempt': True}]
        adjust = lambda xml: _global_adjustments(etree.fromstring(xml), lines)
        percent = adjust(b'<Documento><DscRcgGlobal><TpoMov>D</TpoMov><TpoValor>%</TpoValor>'
                         b'<ValorDR>10</ValorDR></DscRcgGlobal></Documento>')
        self.assertEqual((percent[0]['amount'], percent[0]['is_exempt']), (-1000, False))
        surcharge = adjust(b'<Documento><DscRcgGlobal><TpoMov>R</TpoMov><TpoValor>$</TpoValor>'
                           b'<ValorDR>500</ValorDR><IndExeDR>1</IndExeDR></DscRcgGlobal></Documento>')
        self.assertEqual((surcharge[0]['amount'], surcharge[0]['is_exempt']), (500, True))
        not_billable = adjust(b'<Documento><DscRcgGlobal><TpoMov>D</TpoMov><TpoValor>$</TpoValor>'
                              b'<ValorDR>500</ValorDR><IndExeDR>2</IndExeDR></DscRcgGlobal></Documento>')
        self.assertEqual(not_billable, [])

    def test_payment_method(self):
        self.assertEqual(payment_method_from_xml(dte_xml(1, EMISSION, payment='1').encode()), '1')
        self.assertEqual(payment_method_from_xml(dte_xml(1, EMISSION, payment='2').encode()), '2')
        self.assertFalse(payment_method_from_xml(dte_xml(1, EMISSION, payment=None).encode()))
        self.assertFalse(payment_method_from_xml(b'roto'))


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestParseResponse(TransactionCase):

    def test_commercial_response(self):
        data = parse_response(response_xml(12, state='2'))
        item = data['items'][0]
        self.assertEqual((data['root'], item['kind'], item['folio'], item['issuer_vat']),
                         ('RespuestaDTE', 'commercial', '12', COMPANY_RUT))
        self.assertIn('Rechazado', item['summary'])

    def test_goods_receipt(self):
        raw = (b'<EnvioRecibos xmlns="http://www.sii.cl/SiiDte"><SetRecibos><Recibo><DocumentoRecibo>'
               b'<TipoDoc>33</TipoDoc><Folio>12</Folio><RUTEmisor>76086428-5</RUTEmisor>'
               b'<Recinto>Bodega</Recinto></DocumentoRecibo></Recibo></SetRecibos></EnvioRecibos>')
        item = parse_response(raw)['items'][0]
        self.assertEqual((item['kind'], item['folio']), ('goods', '12'))

    def test_dte_is_not_a_response(self):
        self.assertIsNone(parse_response(envio_xml(dte_xml(1, EMISSION))))
        self.assertIsNone(parse_response(b'roto'))


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestReceptionDate(TransactionCase):

    def test_parse_reception_date(self):
        self.assertEqual(parse_reception_date('24-09-2026 10:15:32'), datetime(2026, 9, 24, 10, 15, 32))
        self.assertEqual(parse_reception_date('2026-09-24 10:15:32'), datetime(2026, 9, 24, 10, 15, 32))
        self.assertEqual(parse_reception_date(' 24-09-2026 '), datetime(2026, 9, 24))
        self.assertIsNone(parse_reception_date('Documento no encontrado'))
        self.assertIsNone(parse_reception_date(None))

    def test_chile_time_zone(self):
        # 23:30 en Chile (UTC-3 en septiembre) es el día siguiente en UTC, y vuelve al mismo día local.
        utc = sii_local_to_utc(datetime(2026, 9, 24, 23, 30))
        self.assertEqual(utc.date(), date(2026, 9, 25))
        self.assertEqual(utc_to_sii_date(utc), date(2026, 9, 24))


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestSiiCodes(TransactionCase):

    def test_verify_codes(self):
        expected = {'DOK': 'verified', 'DNK': 'mismatch', 'MMC': 'modified', 'FAU': 'not_found',
                    'FNA': 'unauthorized', 'FAN': 'annulled', 'ANC': 'annulled', None: 'error', 'XYZ': 'error'}
        for code, state in expected.items():
            with self.subTest(code=code):
                self.assertEqual(verify_state_from_code(code), state)

    def test_client_states(self):
        self.assertEqual(client_state_from_events(set(), False), 'pending')
        self.assertEqual(client_state_from_events(set(), True), 'presumed')
        self.assertEqual(client_state_from_events({'ACD'}, False), 'accepted')
        self.assertEqual(client_state_from_events({'ACD', 'ERM'}, False), 'receipt')
        self.assertEqual(client_state_from_events({'RFT', 'ACD'}, False), 'claimed')

    def test_claim_response(self):
        class Zeep:                                    # imita un objeto de zeep
            def __init__(self, **values):
                self.__dict__.update(values)

            def __getitem__(self, key):
                return self.__dict__[key]

        ok = normalize_claim_response({'errores': [], 'respuesta': Zeep(codResp=0, descResp='OK',
                                                                        listaEventosDoc=None)})
        self.assertTrue(ok['ok'])
        self.assertTrue(normalize_claim_response({'errores': [], 'respuesta': Zeep(
            codResp='7', descResp='Ya registrado', listaEventosDoc=[])})['ok'])
        late = normalize_claim_response({'errores': [], 'respuesta': Zeep(codResp=8, descResp='Plazo',
                                                                          listaEventosDoc=[])})
        self.assertEqual((late['ok'], late['transient']), (False, False))
        self.assertTrue(normalize_claim_response({'errores': ['Sin token'], 'respuesta': ''})['transient'])
        history = normalize_claim_response({'errores': [], 'respuesta': {'codResp': 15, 'descResp': 'Eventos',
            'listaEventosDoc': [{'codEvento': 'ACD', 'descEvento': 'Acepta', 'rutResponsable': '1',
                                 'dvResponsable': '9', 'fechaEvento': '2026-01-01'}]}})
        self.assertEqual([event['code'] for event in history['events']], ['ACD'])
