# -*- coding: utf-8 -*-
"""Documentos tributarios recibidos de proveedores.

Ruta real: models/dte_received.py

Cada registro es un DTE de un sobre recibido. La factura de proveedor se crea
con un botón, en borrador, con una línea por cada detalle del XML: descripción
del proveedor sobre el producto genérico configurado en Ajustes.
"""
from __future__ import annotations

import base64
import logging
from datetime import date

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.tf_dte_cl.models.dte_lines import round_half_up
from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

_logger = logging.getLogger(__name__)

RECEIVED_STATES = [
    ('received', 'Recibido'),
    ('billed', 'Factura creada'),
    ('ignored', 'Descartado'),
]
# Tipos que se registran; el resto del sobre se guarda igual pero no se factura.
BILLABLE_TYPES = ('33', '34', '46', '56', '61')


class TfDteClReceived(models.Model):
    _name = 'tf_dte_cl.received'
    _description = 'Documento tributario recibido'
    _inherit = ['mail.thread']
    _order = 'date desc, id desc'
    _rec_name = 'display_reference'

    envelope_id = fields.Many2one(
        'tf_dte_cl.exchange.envelope', string='Sobre', required=True, ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(related='envelope_id.company_id', store=True, index=True)
    partner_id = fields.Many2one('res.partner', string='Proveedor', index=True)
    issuer_vat = fields.Char(string='RUT emisor', readonly=True, index=True)
    issuer_name = fields.Char(string='Razón social emisor', readonly=True)
    issuer_activity = fields.Char(string='Giro emisor', readonly=True)
    issuer_email = fields.Char(string='Correo del emisor', readonly=True)
    document_type = fields.Char(string='Tipo DTE', readonly=True, index=True)
    document_type_name = fields.Char(string='Documento', compute='_compute_document_type_name', store=True)
    folio = fields.Integer(string='Folio', readonly=True, index=True)
    display_reference = fields.Char(string='Referencia', compute='_compute_display_reference', store=True)
    date = fields.Date(string='Fecha de emisión', readonly=True)
    amount_net = fields.Integer(string='Monto neto', readonly=True)
    amount_exempt = fields.Integer(string='Monto exento', readonly=True)
    amount_tax = fields.Integer(string='IVA', readonly=True)
    amount_total = fields.Integer(string='Monto total', readonly=True)
    state = fields.Selection(RECEIVED_STATES, string='Estado', default='received', readonly=True, index=True)
    xml_file = fields.Binary(string='XML del DTE', attachment=True, readonly=True, copy=False)
    xml_filename = fields.Char(readonly=True)
    move_id = fields.Many2one('account.move', string='Factura de proveedor', readonly=True, copy=False)
    line_ids = fields.One2many('tf_dte_cl.received.line', 'received_id', string='Detalle', readonly=True)

    _sql_constraints = [
        ('document_uniq', 'unique(company_id, issuer_vat, document_type, folio)',
         'Ese documento del proveedor ya está registrado.'),
    ]

    @api.depends('document_type')
    def _compute_document_type_name(self):
        types = self.env['tf_dte_cl.document_type'].with_context(active_test=False)
        for received in self:
            doc_type = types.search([('code', '=', received.document_type)], limit=1)
            received.document_type_name = doc_type.name or received.document_type

    @api.depends('document_type', 'folio')
    def _compute_display_reference(self):
        for received in self:
            received.display_reference = 'T%sF%s' % (received.document_type or '?', received.folio or 0)

    # ------------------------------------------------------------------
    # Registro
    # ------------------------------------------------------------------
    @api.model
    def _tf_dte_cl_base_payload(self, company) -> dict:
        return {
            'Emisor': company._tf_dte_cl_emitter_payload(),
            'firma_electronica': company._tf_dte_cl_signature_payload(),
        }

    @api.model
    def _tf_dte_cl_values(self, document: dict, envelope) -> dict:
        partner = self.env['res.partner'].search([('vat', '=', document['issuer_vat'])], limit=1)
        try:
            emission = date.fromisoformat(document['date']) if document['date'] else False
        except ValueError:
            emission = False
        return {
            'partner_id': partner.id or False,
            'issuer_vat': document['issuer_vat'],
            'issuer_name': document['issuer_name'],
            'issuer_activity': document['issuer_activity'],
            'issuer_email': document['issuer_email'],
            'document_type': document['document_type'],
            'folio': int(document['folio'] or 0),
            'date': emission,
            'amount_net': document['amount_net'],
            'amount_exempt': document['amount_exempt'],
            'amount_tax': document['amount_tax'],
            'amount_total': document['amount_total'],
            'xml_file': base64.b64encode(document['xml'].encode('ISO-8859-1', errors='xmlcharrefreplace')),
            'xml_filename': 'DTE_T%sF%s.xml' % (document['document_type'], document['folio']),
            'line_ids': [fields.Command.create({
                'name': ' '.join(filter(None, [line['name'], line['description']]))[:500] or '/',
                'quantity': line['quantity'],
                'price_unit': line['price_unit'] or (line['amount'] / (line['quantity'] or 1)),
                'amount': line['amount'],
                'is_exempt': line['is_exempt'],
            }) for line in document['lines']],
        }

    # ------------------------------------------------------------------
    # Factura de proveedor
    # ------------------------------------------------------------------
    def _tf_dte_cl_find_partner(self):
        """Busca el proveedor por RUT; si no existe, lo crea con los datos del DTE."""
        self.ensure_one()
        if self.partner_id:
            return self.partner_id
        partner = self.env['res.partner'].search([('vat', '=', self.issuer_vat)], limit=1)
        if not partner:
            partner = self.env['res.partner'].create({
                'name': self.issuer_name or self.issuer_vat,
                'vat': self.issuer_vat,
                'is_company': True,
                'country_id': self.env.ref('base.cl').id,
                'tf_dte_cl_giro': self.issuer_activity,
                'tf_dte_cl_dte_email': self.issuer_email,
            })
        self.partner_id = partner
        return partner

    def action_tf_dte_cl_create_bill(self):
        for received in self:
            received._tf_dte_cl_create_bill()
        if len(self) == 1 and self.move_id:
            return {
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'res_id': self.move_id.id,
                'view_mode': 'form',
            }
        return True

    def _tf_dte_cl_create_bill(self):
        self.ensure_one()
        if self.move_id:
            raise UserError(self.env._('Este documento ya tiene una factura de proveedor.'))
        if self.document_type not in BILLABLE_TYPES:
            raise UserError(self.env._(
                'El documento %s no genera factura de proveedor.', self.document_type_name,
            ))
        company = self.company_id
        product = company.tf_dte_cl_exchange_product_id
        if not product:
            raise UserError(self.env._(
                'Configure el producto para documentos recibidos en Ajustes > Facturación electrónica.'
            ))
        partner = self._tf_dte_cl_find_partner()
        taxes = company.account_purchase_tax_id
        move_type = 'in_refund' if self.document_type == '61' else 'in_invoice'
        move = self.env['account.move'].with_company(company).create({
            'move_type': move_type,
            'partner_id': partner.id,
            'invoice_date': self.date,
            'date': self.date,
            'ref': '%s %s' % (self.document_type_name, self.folio),
            'invoice_line_ids': [fields.Command.create({
                'product_id': product.id,
                'name': line.name,
                'quantity': line.quantity,
                'price_unit': line.price_unit,
                'tax_ids': [fields.Command.set([] if line.is_exempt else taxes.ids)],
            }) for line in self.line_ids],
        })
        self.write({'move_id': move.id, 'state': 'billed'})
        self._tf_dte_cl_attach_xml(move)
        difference = round_half_up(move.amount_total) - self.amount_total
        if difference:
            body = self.env._(
                'El total de la factura (%(odoo)s) no coincide con el del DTE recibido (%(dte)s). '
                'Revise las líneas y los impuestos antes de confirmar.',
                odoo=round_half_up(move.amount_total), dte=self.amount_total,
            )
            move.message_post(body=body)
            self.message_post(body=body)
        else:
            self.message_post(body=self.env._('Factura de proveedor creada en borrador: %s.', move.display_name))
        return move

    def _tf_dte_cl_attach_xml(self, move) -> None:
        self.ensure_one()
        if not self.xml_file:
            return
        self.env['ir.attachment'].create({
            'name': self.xml_filename or '%s.xml' % self.display_reference,
            'datas': self.xml_file,
            'res_model': move._name,
            'res_id': move.id,
            'mimetype': 'application/xml',
        })

    def action_tf_dte_cl_ignore(self):
        self.filtered(lambda r: r.state == 'received').write({'state': 'ignored'})
        return True

    def action_open_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'view_mode': 'form',
        }


class TfDteClReceivedLine(models.Model):
    _name = 'tf_dte_cl.received.line'
    _description = 'Detalle de un documento recibido'
    _order = 'received_id, id'

    received_id = fields.Many2one(
        'tf_dte_cl.received', string='Documento', required=True, ondelete='cascade', index=True,
    )
    name = fields.Char(string='Descripción del proveedor', required=True)
    quantity = fields.Float(string='Cantidad', digits='Product Unit of Measure')
    price_unit = fields.Float(string='Precio unitario', digits='Product Price')
    amount = fields.Integer(string='Monto')
    is_exempt = fields.Boolean(string='Exento')
