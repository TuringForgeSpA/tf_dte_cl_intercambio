# -*- coding: utf-8 -*-
"""Documentos recibidos: registro, verificación, factura de proveedor, aceptación y reclamo.

Ruta real: tests/test_received.py
"""
from collections import namedtuple
from datetime import datetime, time, timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import (
    OTHER_SUPPLIER_RUT,
    SUPPLIER_RUT,
    IntercambioCommon,
    dte_xml,
    envio_xml,
    wrapped_xml,
)

MailAttachment = namedtuple('MailAttachment', 'fname content info')


@tagged('post_install', '-at_install', 'tf_dte_cl_intercambio')
class TestReceived(IntercambioCommon):

    def credit_document(self, folio=500, days_ago=0, discount=0):
        """Factura a crédito recibida en el formato de Acepta (sin carátula)."""
        emission = self.today - timedelta(days=days_ago)
        envelope = self.register(wrapped_xml(dte_xml(folio, emission, payment='2', discount=discount,
                                                     namespace=True)))
        return envelope.received_ids

    # --- registro ------------------------------------------------------------
    def test_register_standard_envelope(self):
        envelope = self.register(envio_xml(dte_xml(120, self.today, payment='1',
                                                   lines=((1, 50000, False), (2, 1500, True)))))
        self.assertEqual(envelope.state, 'received')
        document = envelope.received_ids
        self.assertEqual(len(document), 1)
        self.assertEqual((document.document_type, document.folio, document.amount_total, document.payment_method),
                         ('33', 120, 62500, '1'))
        self.assertFalse(document.sii_claimable)                   # al contado: sin eventos SII
        self.assertFalse(document.sii_deadline)

    def test_wrapped_document_has_no_reception_answer(self):
        document = self.credit_document()
        self.assertEqual(document.envelope_id.state, 'no_reception')
        self.assertTrue(document.sii_claimable)
        self.assertEqual(document.sii_deadline, self.today + timedelta(days=8))

    def test_duplicate_is_skipped_and_not_answered(self):
        raw = envio_xml(dte_xml(121, self.today))
        self.register(raw)
        again = self.register(raw)
        self.assertFalse(again.received_ids)
        self.assertEqual(again.state, 'no_reception')

    def test_other_receiver_is_rejected(self):
        envelope = self.register(envio_xml(dte_xml(122, self.today, receiver=OTHER_SUPPLIER_RUT),
                                           receiver=OTHER_SUPPLIER_RUT))
        self.assertEqual(envelope.state, 'rejected')
        self.assertFalse(envelope.received_ids)

    def test_mail_with_xml(self):
        message = {'attachments': [
            MailAttachment('factura.pdf', b'%PDF-1.4', {}),
            MailAttachment('dte.xml', envio_xml(dte_xml(123, self.today)), {}),
        ]}
        envelope = self.env['tf_dte_cl.exchange.envelope'].message_new(message)
        self.assertEqual((envelope.name, len(envelope.received_ids)), ('dte.xml', 1))

    def test_mail_without_xml(self):
        message = {'subject': 'Consulta', 'body': '<p>Hola</p>', 'email_from': 'alguien@correo.cl',
                   'attachments': []}
        envelope = self.env['tf_dte_cl.exchange.envelope'].message_new(message)
        self.assertEqual(envelope.state, 'error')

    def test_reception_answer(self):
        envelope = self.register(envio_xml(dte_xml(124, self.today)))
        with self.fake_exchange() as calls:
            envelope.action_tf_dte_cl_answer_reception()
        self.assertEqual(calls['reception'], 1)
        self.assertEqual(envelope.state, 'answered')
        self.assertTrue(envelope.response_file)

    # --- verificación en el SII ---------------------------------------------
    def test_verification(self):
        document = self.credit_document()
        with self.fake_sii() as fake:
            fake.document_code = 'DOK'
            document.action_tf_dte_cl_verify()
        self.assertEqual(document.sii_verify_state, 'verified')

    def test_not_received_blocks_bill(self):
        document = self.credit_document()
        with self.fake_sii() as fake:
            fake.document_code = 'FAU'
            document.action_tf_dte_cl_verify()
        self.assertEqual(document.sii_verify_state, 'not_found')
        with self.assertUserError('no recibido'):
            document.action_tf_dte_cl_create_bill()
        self.assertFalse(document.move_id)

    # --- factura de proveedor -----------------------------------------------
    def test_create_bill_with_global_discount(self):
        document = self.credit_document(discount=2201)            # 8.803 - 2.201 = 6.602 neto
        with self.fake_sii() as fake:
            fake.document_code = 'DOK'
            document.action_tf_dte_cl_verify()
        document.action_tf_dte_cl_create_bill()
        bill = document.move_id
        self.assertEqual((bill.move_type, bill.state, bill.invoice_date), ('in_invoice', 'draft', document.date))
        self.assertEqual(bill.partner_id.vat, SUPPLIER_RUT)       # proveedor creado desde el XML
        self.assertEqual(len(bill.invoice_line_ids), 2)
        self.assertEqual(bill.amount_total, document.amount_total)
        self.assertEqual(document.amount_total, 7856)
        self.assertTrue(self.env['ir.attachment'].search_count([
            ('res_model', '=', 'account.move'), ('res_id', '=', bill.id), ('name', '=like', '%.xml'),
        ]))
        self.assertEqual(document.state, 'billed')
        with self.assertRaises(UserError):
            document._tf_dte_cl_create_bill()                     # no se factura dos veces

    # --- aceptación y reclamo -----------------------------------------------
    def test_accept_registers_acd_and_erm(self):
        document = self.credit_document()
        with self.fake_exchange() as calls:
            document.action_tf_dte_cl_accept()
        self.assertEqual(calls['register'], ['ACD', 'ERM'])
        self.assertTrue(document.sii_accepted and document.sii_receipt)

    def test_claim_blocks_acceptance(self):
        document = self.credit_document()
        wizard = self.env['tf_dte_cl.received.claim'].create({
            'received_ids': [Command.set(document.ids)],
            'claim_type': 'RFT',
            'reason': 'No se recibió el servicio',
        })
        with self.fake_exchange() as calls:
            wizard.action_claim()
        self.assertEqual((calls['register'], document.sii_claim), (['RFT'], 'RFT'))
        with self.fake_exchange(), self.assertUserError('reclamado'):
            document.action_tf_dte_cl_accept()

    def test_cash_invoice_cannot_be_accepted(self):
        envelope = self.register(envio_xml(dte_xml(125, self.today, payment='1')))
        with self.fake_exchange() as calls, self.assertUserError('contado'):
            envelope.received_ids.action_tf_dte_cl_accept()
        self.assertEqual(calls['register'], [])

    def test_sii_refusal_is_reported(self):
        document = self.credit_document()
        with self.fake_exchange(register_ok=False, register_code=8), self.assertUserError('SII no registró'):
            document.action_tf_dte_cl_accept()
        self.assertFalse(document.sii_accepted)

    # --- notas enlazadas a la factura original ------------------------------
    def test_credit_note_is_linked_to_original_bill(self):
        invoice = self.credit_document(folio=700)
        invoice.action_tf_dte_cl_create_bill()
        note = self.register(wrapped_xml(dte_xml(
            12, self.today, doc_type='61', lines=((1, 1000, False),), namespace=True,
            references=(('33', '700', self.today.isoformat(), '3', 'Corrige montos'),),
        ))).received_ids
        self.assertEqual(note.origin_id, invoice)
        note.action_tf_dte_cl_create_bill()
        refund = note.move_id
        self.assertEqual(refund.move_type, 'in_refund')
        self.assertEqual(refund.reversed_entry_id, invoice.move_id)
        self.assertIn('ref.', refund.ref)

    def test_credit_note_without_original(self):
        note = self.register(wrapped_xml(dte_xml(
            13, self.today, doc_type='61', lines=((1, 1000, False),), namespace=True,
            references=(('33', '999', self.today.isoformat(), '1', 'Anula'),),
        ))).received_ids
        self.assertFalse(note.origin_id)
        self.assertEqual(len(note.reference_ids), 1)
        note.action_tf_dte_cl_create_bill()
        self.assertFalse(note.move_id.reversed_entry_id)

    # --- fecha de recepción en el SII ----------------------------------------
    def test_reception_date_sets_real_deadline(self):
        document = self.credit_document(days_ago=3)
        self.assertTrue(document.sii_deadline_estimated)
        received_local = datetime.combine(self.today - timedelta(days=1), time(23, 30))
        with self.fake_exchange(reception_date=received_local) as calls:
            document._tf_dte_cl_update_reception_date()
            document._tf_dte_cl_update_reception_date()           # ya conocida: no se vuelve a consultar
        self.assertEqual(calls['reception_date'], 1)
        self.assertFalse(document.sii_deadline_estimated)
        self.assertEqual(document.sii_deadline, self.today - timedelta(days=1) + timedelta(days=8))

    def test_verification_queries_reception_date(self):
        document = self.credit_document()
        with self.fake_sii() as fake, self.fake_exchange(reception_date=datetime.combine(self.today, time(9))) as calls:
            fake.document_code = 'DOK'
            document.action_tf_dte_cl_verify()
        self.assertEqual(calls['reception_date'], 1)
        self.assertTrue(document.sii_reception_date)

    # --- guías de despacho y recepciones -------------------------------------
    def incoming_picking(self, partner):
        picking_type = self.env.ref('stock.picking_type_in')
        return self.env['stock.picking'].create({
            'picking_type_id': picking_type.id,
            'partner_id': partner.id,
            'location_id': self.env.ref('stock.stock_location_suppliers').id,
            'location_dest_id': picking_type.default_location_dest_id.id,
            'scheduled_date': fields.Datetime.now(),
        })

    def supplier(self):
        return self.env['res.partner'].create({
            'name': 'Proveedor de Prueba SpA', 'vat': SUPPLIER_RUT, 'is_company': True,
            'country_id': self.chile.id,
        })

    def guide(self, folio, references=()):
        return self.register(wrapped_xml(dte_xml(
            folio, self.today, doc_type='52', payment=None, namespace=True, references=references,
        ))).received_ids

    def test_guide_linked_to_single_receipt(self):
        picking = self.incoming_picking(self.supplier())
        guide = self.guide(900)
        self.assertEqual(guide.picking_id, picking)
        self.assertEqual(picking.tf_dte_cl_received_guide_ids, guide)

    def test_guide_not_linked_when_ambiguous(self):
        supplier = self.supplier()
        self.incoming_picking(supplier)
        self.incoming_picking(supplier)
        guide = self.guide(901)
        self.assertFalse(guide.picking_id)
        with self.assertUserError('Selecciónela a mano'):
            guide.action_tf_dte_cl_match_picking()

    def test_guide_linked_by_purchase_order(self):
        if 'purchase_id' not in self.env['stock.picking']._fields:
            self.skipTest('El módulo de Compras no está instalado.')
        supplier = self.supplier()
        order = self.env['purchase.order'].create({'partner_id': supplier.id})
        picking = self.incoming_picking(supplier)
        picking.purchase_id = order
        self.incoming_picking(supplier)                           # otra recepción del mismo proveedor
        guide = self.guide(902, references=(('801', order.name, self.today.isoformat(), '', 'OC'),))
        self.assertEqual(guide.picking_id, picking)

    # --- aviso de plazo ------------------------------------------------------
    def test_deadline_alert(self):
        document = self.credit_document(days_ago=7)               # vence mañana
        Received = self.env['tf_dte_cl.received']
        Received._cron_tf_dte_cl_deadline_alerts()
        Received._cron_tf_dte_cl_deadline_alerts()                # no se duplica
        self.assertEqual(len(document.activity_ids), 1)
        self.assertEqual(document.activity_ids.user_id, self.responsible)
        self.assertEqual(document.activity_ids.date_deadline, document.sii_deadline)
        with self.fake_exchange():
            document.action_tf_dte_cl_accept()
        self.assertFalse(document.activity_ids)                   # se cierra al responder

    def test_no_alert_far_from_deadline(self):
        document = self.credit_document(days_ago=1)
        self.env['tf_dte_cl.received']._cron_tf_dte_cl_deadline_alerts()
        self.assertFalse(document.activity_ids)
