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
from datetime import date, timedelta

from lxml import etree

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.tf_dte_cl.models.dte_lines import round_half_up
from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

from .sii_client import CLAIM_ACTIONS, CLAIM_DOCUMENT_TYPES

_logger = logging.getLogger(__name__)

RECEIVED_STATES = [
    ('received', 'Recibido'),
    ('billed', 'Factura creada'),
    ('ignored', 'Descartado'),
]
# Tipos que se registran; el resto del sobre se guarda igual pero no se factura.
BILLABLE_TYPES = ('33', '34', '46', '56', '61')
# Ley 19.983 (modificada por la Ley 20.956): 8 días corridos desde la recepción en el SII.
CLAIM_DAYS = 8
# FmaPago del DTE. El SII no admite eventos en documentos al contado o sin costo
# (respuesta 27: "No se puede registrar un evento ... de un DTE pagado al contado o gratuito").
PAYMENT_METHODS = [('1', 'Contado'), ('2', 'Crédito'), ('3', 'Sin costo (entrega gratuita)')]
NO_CLAIM_PAYMENT_METHODS = ('1', '3')


def payment_method_from_xml(raw: bytes) -> str | bool:
    """Forma de pago (FmaPago) declarada en el XML de un DTE, o False si no la informa."""
    if not raw:
        return False
    try:
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return False
    for element in root.iter():
        if isinstance(element.tag, str) and etree.QName(element).localname == 'FmaPago':
            value = (element.text or '').strip()
            return value if value in dict(PAYMENT_METHODS) else False
    return False
CLAIM_TYPES = [('RCD', CLAIM_ACTIONS['RCD']), ('RFP', CLAIM_ACTIONS['RFP']), ('RFT', CLAIM_ACTIONS['RFT'])]


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

    # Aceptación y reclamo ante el SII
    payment_method = fields.Selection(
        PAYMENT_METHODS, string='Forma de pago', compute='_compute_payment_method', store=True,
        help='Forma de pago declarada en el DTE (FmaPago).',
    )
    # Almacenado: se usa en el dominio del filtro "Sin respuesta SII".
    sii_claimable = fields.Boolean(
        string='Admite aceptación o reclamo', compute='_compute_sii_claimable', store=True,
    )
    sii_deadline = fields.Date(
        string='Plazo SII (estimado)', compute='_compute_sii_deadline', store=True,
        help='8 días corridos desde la recepción en el SII. Se calcula desde la fecha de emisión, que es '
             'igual o anterior a la recepción, por lo que el plazo real nunca es más corto que este.',
    )
    sii_accepted = fields.Boolean(string='Contenido aceptado', readonly=True, copy=False)
    sii_receipt = fields.Boolean(string='Acuse de recibo otorgado', readonly=True, copy=False)
    sii_claim = fields.Selection(CLAIM_TYPES, string='Reclamo', readonly=True, copy=False)
    sii_message = fields.Char(string='Respuesta del SII', readonly=True, copy=False)
    sii_events = fields.Text(string='Eventos en el SII', readonly=True, copy=False)
    sii_last_query = fields.Datetime(string='Última consulta SII', readonly=True, copy=False)

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

    @api.depends('xml_file')
    def _compute_payment_method(self):
        for received in self:
            xml = received.with_context(bin_size=False).xml_file
            received.payment_method = payment_method_from_xml(base64.b64decode(xml)) if xml else False

    @api.depends('document_type', 'payment_method')
    def _compute_sii_claimable(self):
        for received in self:
            received.sii_claimable = (
                received.document_type in CLAIM_DOCUMENT_TYPES
                and received.payment_method not in NO_CLAIM_PAYMENT_METHODS
            )

    @api.depends('date', 'sii_claimable')
    def _compute_sii_deadline(self):
        for received in self:
            received.sii_deadline = (
                received.sii_claimable and received.date and received.date + timedelta(days=CLAIM_DAYS)
            ) or False

    # ------------------------------------------------------------------
    # Aceptación y reclamo ante el SII
    # ------------------------------------------------------------------
    def _tf_dte_cl_claim_payload(self, action: str | None = None) -> dict:
        self.ensure_one()
        claim = {'RUTEmisor': normalize_rut(self.issuer_vat), 'TipoDTE': self.document_type, 'Folio': self.folio}
        if action:
            claim['Claim'] = action
        return dict(self._tf_dte_cl_base_payload(self.company_id), DTEClaim=[claim])

    def _tf_dte_cl_register_action(self, action: str) -> dict:
        """Registra una acción en el SII y deja constancia; lanza UserError si falla."""
        self.ensure_one()
        if not self.sii_claimable:
            if self.payment_method in NO_CLAIM_PAYMENT_METHODS:
                raise UserError(self.env._(
                    '%s fue emitida al contado o sin costo: el SII no admite aceptarla ni reclamarla.',
                    self.display_reference,
                ))
            raise UserError(self.env._(
                'El SII solo registra aceptaciones y reclamos de facturas (33, 34 y 43).'
            ))
        result = self.env['tf_dte_cl.sii.client'].tf_dte_cl_register_claim(self._tf_dte_cl_claim_payload(action))
        message = '%s: %s' % (result.get('code'), result.get('description') or '')
        self.sii_message = message
        if not result['ok']:
            hint = self.env._(' Reintente más tarde.') if result.get('transient') else ''
            raise UserError(self.env._(
                'El SII no registró "%(action)s" para %(doc)s: %(msg)s%(hint)s',
                action=CLAIM_ACTIONS[action], doc=self.display_reference, msg=message, hint=hint,
            ))
        vals = {'ACD': {'sii_accepted': True}, 'ERM': {'sii_receipt': True}}.get(action, {'sii_claim': action})
        self.write(vals)
        self.message_post(body=self.env._('Registrado en el SII: %(action)s (%(msg)s).',
                                          action=CLAIM_ACTIONS[action], msg=message))
        return result

    def action_tf_dte_cl_accept(self):
        """Acepta el contenido y otorga el acuse de recibo (ACD y luego ERM)."""
        for received in self:
            if received.sii_claim:
                raise UserError(self.env._('%s ya fue reclamado; no se puede aceptar.', received.display_reference))
            if not received.sii_accepted:
                received._tf_dte_cl_register_action('ACD')
            if not received.sii_receipt:
                received._tf_dte_cl_register_action('ERM')
        return True

    def action_tf_dte_cl_open_claim(self):
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Reclamar documento'),
            'res_model': 'tf_dte_cl.received.claim',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_received_ids': self.ids},
        }

    def action_tf_dte_cl_query_sii(self):
        """Consulta en el SII los eventos del documento y actualiza su estado."""
        client = self.env['tf_dte_cl.sii.client']
        for received in self.filtered('sii_claimable'):
            result = client.tf_dte_cl_claim_history(received._tf_dte_cl_claim_payload())
            vals = {'sii_last_query': fields.Datetime.now(),
                    'sii_message': '%s: %s' % (result.get('code'), result.get('description') or '')}
            if result['ok']:
                codes = {event['code'] for event in result['events']}
                vals['sii_events'] = '\n'.join(
                    '%(date)s  %(code)s  %(description)s  (%(rut)s)' % event for event in result['events']
                ) or False
                vals['sii_accepted'] = received.sii_accepted or 'ACD' in codes
                vals['sii_receipt'] = received.sii_receipt or 'ERM' in codes
                claimed = [code for code in ('RCD', 'RFP', 'RFT') if code in codes]
                if claimed:
                    vals['sii_claim'] = claimed[0]
            received.write(vals)
        return True

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


class TfDteClReceivedClaim(models.TransientModel):
    _name = 'tf_dte_cl.received.claim'
    _description = 'Reclamo de un documento recibido'

    received_ids = fields.Many2many('tf_dte_cl.received', string='Documentos')
    claim_type = fields.Selection(CLAIM_TYPES, string='Tipo de reclamo', required=True, default='RCD')
    reason = fields.Text(
        string='Motivo', required=True,
        help='No se envía al SII (el servicio no recibe un motivo): queda registrado en el historial del documento.',
    )

    def action_claim(self):
        self.ensure_one()
        for received in self.received_ids:
            if received.sii_accepted or received.sii_receipt:
                raise UserError(self.env._(
                    '%s ya fue aceptado o tiene acuse de recibo; el SII no permite reclamarlo.',
                    received.display_reference,
                ))
            received._tf_dte_cl_register_action(self.claim_type)
            received.message_post(body=self.env._('Motivo del reclamo: %s', self.reason))
            if received.move_id:
                received.move_id.message_post(body=self.env._(
                    'El documento de origen fue reclamado ante el SII (%(type)s): %(reason)s',
                    type=dict(CLAIM_TYPES)[self.claim_type], reason=self.reason,
                ))
        return {'type': 'ir.actions.act_window_close'}
