# -*- coding: utf-8 -*-
"""Documentos emitidos: envío al cliente y respuesta del cliente.

Ruta real: tests/test_emitted.py
"""
from datetime import timedelta
from unittest.mock import patch

from odoo.tests import tagged

from .common import IntercambioCommon, response_xml


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestEmitted(IntercambioCommon):

    def fake_pdf(self):
        """El PDF no se genera en las pruebas (evita depender de wkhtmltopdf)."""
        report_class = type(self.env['ir.actions.report'])
        return patch.object(report_class, '_render_qweb_pdf',
                            lambda report, report_ref, res_ids=None, data=None: (b'%PDF-1.4 prueba', 'pdf'))

    # --- envío al cliente ----------------------------------------------------
    def test_send_to_receiver(self):
        move = self.credit_invoice_accepted()
        with self.fake_sii(), self.fake_pdf():
            self.assertEqual(move._tf_dte_cl_exchange_send(), 'sent')
        self.assertEqual(move.tf_dte_cl_exchange_email, 'dte@cliente.cl')
        attachments = self.env['ir.attachment'].search([('res_model', '=', 'account.move'), ('res_id', '=', move.id)])
        self.assertTrue(attachments.filtered(lambda a: a.mimetype == 'application/xml'))
        self.assertFalse(attachments.filtered(lambda a: a.mimetype == 'application/pdf'),
                         'El PDF no debe quedar adjunto a la factura (abriría el visor de documentos).')

    def test_send_is_not_repeated(self):
        move = self.credit_invoice_accepted()
        with self.fake_sii() as fake, self.fake_pdf():
            move._tf_dte_cl_exchange_send()
            move.action_tf_dte_cl_exchange_send()
        self.assertEqual(fake.calls['envelope'], 1)              # el sobre al cliente se arma una sola vez

    def test_receiver_without_exchange_email(self):
        self.partner.tf_dte_cl_dte_email = False
        move = self.credit_invoice_accepted()
        with self.fake_sii(), self.fake_pdf():
            self.assertEqual(move._tf_dte_cl_exchange_send(), 'not_applicable')

    # --- respuesta del cliente en el SII ------------------------------------
    def test_client_claim_creates_activity(self):
        move = self.credit_invoice_accepted()
        with self.fake_exchange(events=('RCD',)):
            self.assertEqual(move._tf_dte_cl_query_client_events(), 'claimed')
        self.assertEqual(move.tf_dte_cl_client_claim, 'RCD')
        self.assertTrue(move.activity_ids)

    def test_client_acceptance(self):
        move = self.credit_invoice_accepted()
        with self.fake_exchange(events=('ACD', 'ERM')):
            self.assertEqual(move._tf_dte_cl_query_client_events(), 'receipt')
        self.assertFalse(move.activity_ids)

    def test_presumed_receipt_after_deadline(self):
        move = self.credit_invoice_accepted()
        move.tf_dte_cl_emission_date = self.today - timedelta(days=15)
        with self.fake_exchange():
            self.assertEqual(move._tf_dte_cl_query_client_events(), 'presumed')

    def test_cash_invoice_not_applicable(self):
        with self.fake_sii():
            move = self.create_invoice()
            move.action_post()
            move._tf_dte_cl_send()
            move._tf_dte_cl_query()
        with self.fake_exchange() as calls:
            self.assertEqual(move._tf_dte_cl_query_client_events(), 'not_applicable')
        self.assertEqual(calls['history'], 0)                    # no se consulta al SII

    # --- respuestas por correo ----------------------------------------------
    def test_client_mail_response_is_linked(self):
        move = self.credit_invoice_accepted()
        envelope = self.register(response_xml(move.tf_dte_cl_folio, state='0'), 'respuesta.xml')
        self.assertEqual(envelope.state, 'client_response')
        self.assertEqual(envelope.response_move_ids, move)
        self.assertIn('Aceptado', move.tf_dte_cl_client_mail_response)
