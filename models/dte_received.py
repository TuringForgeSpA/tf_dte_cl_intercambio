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

import pytz
from lxml import etree

from odoo import api, fields, models
from odoo.exceptions import UserError

from odoo.addons.tf_dte_cl.models.dte_lines import round_half_up
from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

from .sii_client import CLAIM_ACTIONS, CLAIM_DOCUMENT_TYPES

# Referencias que apuntan a un documento tributario del mismo proveedor (notas sobre facturas).
ORIGIN_DOCUMENT_TYPES = ('33', '34', '46', '56', '61')
# Código del SII para una orden de compra en las referencias (TpoDocRef).
PURCHASE_ORDER_REFERENCE = '801'
# Ventana de fechas para asociar una guía recibida con una recepción del mismo proveedor.
PICKING_MATCH_DAYS = 7
# El SII informa las fechas en hora de Chile; Odoo guarda las fechas con hora en UTC.
SII_TZ = pytz.timezone('America/Santiago')


def sii_local_to_utc(value):
    """Fecha y hora local de Chile (sin zona) a UTC sin zona, como la guarda Odoo."""
    return SII_TZ.localize(value).astimezone(pytz.utc).replace(tzinfo=None)


def utc_to_sii_date(value):
    """Fecha (día) en Chile de una fecha y hora UTC guardada por Odoo."""
    return pytz.utc.localize(value).astimezone(SII_TZ).date()

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


# Verificación del documento recibido con la consulta de estado de DTE del SII
# (QueryEstDte, manual OI2004_CEDTE_MDE_1.10).
VERIFY_STATES = [
    ('verified', 'Verificado en el SII'),
    ('mismatch', 'Datos no coinciden con el SII'),
    ('modified', 'Modificado por una nota'),
    ('not_found', 'No recibido por el SII'),
    ('unauthorized', 'Emisor no autorizado'),
    ('annulled', 'Anulado'),
    ('error', 'Error de consulta'),
]
VERIFY_CODES = {
    'DOK': 'verified', 'DNK': 'mismatch',
    'TMD': 'modified', 'TMC': 'modified', 'MMD': 'modified', 'MMC': 'modified',
    'FAU': 'not_found', 'FNA': 'unauthorized', 'EMP': 'unauthorized',
    'FAN': 'annulled', 'AND': 'annulled', 'ANC': 'annulled',
}
# Estados que impiden crear la factura de proveedor.
VERIFY_BLOCKING = ('not_found', 'unauthorized', 'annulled')
# Estados que se vuelven a consultar, y hasta cuántos días desde la emisión.
VERIFY_RETRY = (False, 'error', 'not_found')
VERIFY_RETRY_DAYS = 10
# Prefijo del resumen de las actividades de aviso de plazo (para cerrarlas solas).
ALERT_PREFIX = 'Plazo SII por vencer:'
# Tipos que admite la consulta del cliente SII de tf_dte_cl.
VERIFIABLE_TYPES = ('33', '34', '52', '56', '61')


def verify_state_from_code(code: str | None) -> str:
    """Estado de verificación según el código de QueryEstDte. No depende de Odoo."""
    return VERIFY_CODES.get(code or '', 'error')


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
    _inherit = ['mail.thread', 'mail.activity.mixin']
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

    # Verificación en el SII
    sii_verify_state = fields.Selection(VERIFY_STATES, string='Verificación SII', readonly=True, copy=False,
                                        index=True)
    sii_verify_detail = fields.Char(string='Detalle de la verificación', readonly=True, copy=False)
    sii_verify_date = fields.Datetime(string='Verificado el', readonly=True, copy=False)

    # Aceptación y reclamo ante el SII
    payment_method = fields.Selection(
        PAYMENT_METHODS, string='Forma de pago', compute='_compute_payment_method', store=True,
        help='Forma de pago declarada en el DTE (FmaPago).',
    )
    # Almacenado: se usa en el dominio del filtro "Sin respuesta SII".
    sii_claimable = fields.Boolean(
        string='Admite aceptación o reclamo', compute='_compute_sii_claimable', store=True,
    )
    sii_reception_date = fields.Datetime(
        string='Recepción en el SII', readonly=True, copy=False,
        help='Fecha y hora en que el SII recibió el documento (consultarFechaRecepcionSii).',
    )
    sii_deadline = fields.Date(
        string='Plazo SII', compute='_compute_sii_deadline', store=True,
        help='8 días corridos desde la recepción en el SII. Mientras no se conoce la fecha de recepción, '
             'se estima desde la fecha de emisión, que es igual o anterior: el plazo real nunca es más corto.',
    )
    sii_deadline_estimated = fields.Boolean(
        string='Plazo estimado', compute='_compute_sii_deadline', store=True,
        help='El plazo se calculó desde la emisión porque aún no se conoce la fecha de recepción en el SII.',
    )
    reference_ids = fields.One2many(
        'tf_dte_cl.received.reference', 'received_id', string='Referencias', readonly=True,
    )
    origin_id = fields.Many2one(
        'tf_dte_cl.received', string='Documento de origen', compute='_compute_origin_id',
        help='Documento recibido del mismo proveedor al que se refiere esta nota.',
    )
    picking_id = fields.Many2one(
        'stock.picking', string='Recepción', copy=False, index=True, check_company=True,
        domain="[('picking_type_code', '=', 'incoming'), ('company_id', '=', company_id)]",
        help='Recepción de inventario asociada a esta guía de despacho.',
    )
    sii_accepted = fields.Boolean(string='Contenido aceptado', readonly=True, copy=False)
    sii_receipt = fields.Boolean(string='Acuse de recibo otorgado', readonly=True, copy=False)
    sii_claim = fields.Selection(CLAIM_TYPES, string='Reclamo', readonly=True, copy=False)
    sii_message = fields.Char(string='Respuesta del SII', readonly=True, copy=False)
    sii_events = fields.Text(string='Eventos en el SII', readonly=True, copy=False)
    sii_last_query = fields.Datetime(string='Última consulta SII', readonly=True, copy=False)
    sii_alert_sent = fields.Boolean(string='Aviso de plazo enviado', readonly=True, copy=False)

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

    @api.depends('date', 'sii_claimable', 'sii_reception_date')
    def _compute_sii_deadline(self):
        for received in self:
            base = utc_to_sii_date(received.sii_reception_date) if received.sii_reception_date else received.date
            received.sii_deadline = (
                received.sii_claimable and base and base + timedelta(days=CLAIM_DAYS)
            ) or False
            received.sii_deadline_estimated = bool(received.sii_deadline and not received.sii_reception_date)

    @api.depends('reference_ids', 'issuer_vat', 'company_id')
    def _compute_origin_id(self):
        for received in self:
            origin = self.browse()
            for reference in received.reference_ids:
                if reference.document_type in ORIGIN_DOCUMENT_TYPES and reference.folio.isdigit():
                    origin = self.search([
                        ('company_id', '=', received.company_id.id),
                        ('issuer_vat', '=', received.issuer_vat),
                        ('document_type', '=', reference.document_type),
                        ('folio', '=', int(reference.folio)),
                        ('id', '!=', received._origin.id or received.id),
                    ], limit=1)
                    if origin:
                        break
            received.origin_id = origin

    # ------------------------------------------------------------------
    # Verificación en el SII
    # ------------------------------------------------------------------
    def _tf_dte_cl_verify_payload(self) -> dict:
        """Consulta de estado de un DTE recibido: el emisor es el proveedor.

        La librería toma el RUT del emisor del bloque Emisor y el RUT consultante
        del certificado; el receptor (esta compañía) va en el documento.
        """
        self.ensure_one()
        company = self.company_id
        return {
            'Emisor': {'RUTEmisor': normalize_rut(self.issuer_vat), 'Modo': company.tf_dte_cl_environment},
            'firma_electronica': company._tf_dte_cl_signature_payload(),
            'Documento': [{
                'TipoDTE': int(self.document_type),
                'documentos': [{
                    'Folio': self.folio,
                    'FchEmis': self.date,
                    'Receptor': {'RUTRecep': normalize_rut(company.vat)},
                    'MntTotal': self.amount_total,
                }],
            }],
        }

    def _tf_dte_cl_verify(self) -> str:
        self.ensure_one()
        if self.document_type not in VERIFIABLE_TYPES:
            return self.sii_verify_state
        result = self.env['tf_dte_cl.sii.client'].tf_dte_cl_query_document(self._tf_dte_cl_verify_payload())
        state = verify_state_from_code(result.code) if result.ok else 'error'
        previous = self.sii_verify_state
        self.write({
            'sii_verify_state': state,
            'sii_verify_detail': ' '.join(filter(None, [result.code, result.detail]))[:250] or False,
            'sii_verify_date': fields.Datetime.now(),
        })
        if state != previous and state not in ('error', 'verified'):
            self.message_post(body=self.env._(
                'Verificación en el SII: %(state)s. %(detail)s',
                state=dict(VERIFY_STATES)[state], detail=self.sii_verify_detail or '',
            ))
        if state in ('verified', 'mismatch', 'modified'):
            self._tf_dte_cl_update_reception_date()
        return state

    def _tf_dte_cl_update_reception_date(self) -> None:
        """Consulta en el SII la fecha de recepción, para calcular el plazo real de 8 días."""
        for received in self.filtered(lambda r: r.sii_claimable and not r.sii_reception_date):
            result = self.env['tf_dte_cl.sii.client'].tf_dte_cl_reception_date(received._tf_dte_cl_claim_payload())
            if result['ok']:
                received.sii_reception_date = sii_local_to_utc(result['date'])
            elif not result['transient']:
                _logger.info('Fecha de recepción de %s no disponible: %s',
                             received.display_reference, result['description'])

    def action_tf_dte_cl_verify(self):
        for received in self:
            received._tf_dte_cl_verify()
        return True

    @api.model
    def _cron_tf_dte_cl_verify(self, limit=50):
        """Verifica en el SII los documentos pendientes; "no recibido" se reintenta unos días.

        También completa la fecha de recepción de los documentos ya verificados que
        todavía están dentro del plazo de reclamo.
        """
        since = fields.Date.context_today(self) - timedelta(days=VERIFY_RETRY_DAYS)
        documents = self.search([
            ('sii_verify_state', 'in', list(VERIFY_RETRY)),
            ('date', '>=', since),
            ('document_type', 'in', list(VERIFIABLE_TYPES)),
        ], order='sii_verify_date asc nulls first, id', limit=limit)
        pending_dates = self.search([
            ('sii_claimable', '=', True),
            ('sii_reception_date', '=', False),
            ('sii_verify_state', 'in', ['verified', 'mismatch', 'modified']),
            ('sii_deadline', '>=', fields.Date.context_today(self)),
        ], limit=limit)
        for document_id in pending_dates.ids:
            try:
                with self.env.cr.savepoint():
                    self.browse(document_id)._tf_dte_cl_update_reception_date()
            except Exception:  # noqa: BLE001 - un documento no debe detener el lote
                _logger.exception('Error al consultar la fecha de recepción del documento %s', document_id)
        for document_id in documents.ids:
            try:
                with self.env.cr.savepoint():
                    self.browse(document_id)._tf_dte_cl_verify()
            except Exception:  # noqa: BLE001 - un documento no debe detener el lote
                self.env.cr.rollback()
                _logger.exception('Error al verificar en el SII el documento recibido %s', document_id)
            if not self.env.registry.in_test_mode():
                self.env.cr.commit()

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
        self._tf_dte_cl_close_alert(self.env._('Registrado en el SII: %s.', CLAIM_ACTIONS[action]))
        self.message_post(body=self.env._('Registrado en el SII: %(action)s (%(msg)s).',
                                          action=CLAIM_ACTIONS[action], msg=message))
        return result

    # ------------------------------------------------------------------
    # Aviso de plazo
    # ------------------------------------------------------------------
    def _tf_dte_cl_alert_user(self):
        """Creador de la factura de proveedor; si no la hay, el responsable de Ajustes; si no, el administrador."""
        self.ensure_one()
        bot = self.env.ref('base.user_root', raise_if_not_found=False)
        creator = self.move_id.create_uid
        if creator and creator != bot and creator.active:
            return creator
        responsible = self.company_id.tf_dte_cl_exchange_responsible_id
        if responsible and responsible.active:
            return responsible
        return self.env.ref('base.user_admin')

    def _tf_dte_cl_needs_answer(self) -> bool:
        self.ensure_one()
        return bool(self.sii_claimable and self.state != 'ignored'
                    and not (self.sii_accepted or self.sii_receipt or self.sii_claim))

    def _tf_dte_cl_close_alert(self, feedback: str) -> None:
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for received in self:
            activities = received.activity_ids.filtered(
                lambda a: a.activity_type_id == todo and (a.summary or '').startswith(ALERT_PREFIX)
            )
            if activities:
                activities.action_feedback(feedback=feedback)

    @api.model
    def _cron_tf_dte_cl_deadline_alerts(self):
        """Crea una actividad por cada factura recibida cuyo plazo SII está por vencer sin respuesta."""
        today = fields.Date.context_today(self)
        for company in self.env['res.company'].search([]):
            days = max(company.tf_dte_cl_exchange_alert_days, 0)
            documents = self.search([
                ('company_id', '=', company.id),
                ('sii_claimable', '=', True),
                ('sii_alert_sent', '=', False),
                ('state', '!=', 'ignored'),
                ('sii_accepted', '=', False), ('sii_receipt', '=', False), ('sii_claim', '=', False),
                ('sii_deadline', '>=', today),
                ('sii_deadline', '<=', today + timedelta(days=days)),
            ])
            for received in documents:
                received.activity_schedule(
                    'mail.mail_activity_data_todo',
                    date_deadline=received.sii_deadline,
                    summary='%s %s' % (ALERT_PREFIX, received.display_reference),
                    note=self.env._(
                        'El plazo para aceptar o reclamar %(doc)s de %(partner)s vence el %(date)s '
                        '(estimado). Pasado ese plazo sin reclamo, el acuse de recibo se presume y ya no '
                        'se puede reclamar.',
                        doc=received.display_reference,
                        partner=received.partner_id.display_name or received.issuer_name,
                        date=received.sii_deadline,
                    ),
                    user_id=received._tf_dte_cl_alert_user().id,
                )
                received.sii_alert_sent = True

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
        self._tf_dte_cl_update_reception_date()
        return self._tf_dte_cl_query_sii_events()

    def _tf_dte_cl_query_sii_events(self):
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
            if not received._tf_dte_cl_needs_answer():
                received._tf_dte_cl_close_alert(self.env._('Respuesta registrada en el SII.'))
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
            'reference_ids': [fields.Command.create({
                'document_type': reference['document_type'],
                'folio': reference['folio'],
                'date': _parse_date(reference['date']),
                'code': reference['code'],
                'reason': (reference['reason'] or '')[:90],
            }) for reference in document.get('references') or []],
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
        if self.sii_verify_state in VERIFY_BLOCKING:
            raise UserError(self.env._(
                'No se puede crear la factura de %(doc)s: %(state)s en el SII. %(detail)s',
                doc=self.display_reference, state=dict(VERIFY_STATES)[self.sii_verify_state].lower(),
                detail=self.sii_verify_detail or '',
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
        origin = self.origin_id
        origin_move = origin.move_id
        ref = '%s %s' % (self.document_type_name, self.folio)
        if origin:
            ref = '%s (ref. %s %s)' % (ref, origin.document_type_name, origin.folio)
        extra = {}
        if move_type == 'in_refund' and origin_move:
            extra['reversed_entry_id'] = origin_move.id
        move = self.env['account.move'].with_company(company).create({
            **extra,
            'move_type': move_type,
            'partner_id': partner.id,
            'invoice_date': self.date,
            'date': self.date,
            'ref': ref,
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
        self._tf_dte_cl_link_origin(move)
        if self.sii_verify_state != 'verified':
            move.message_post(body=self.env._(
                'Atención: el documento de origen no está verificado en el SII (%s). '
                'Revíselo antes de confirmar la factura.',
                dict(VERIFY_STATES).get(self.sii_verify_state, self.env._('sin verificar')),
            ))
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

    def _tf_dte_cl_link_origin(self, move) -> None:
        """Deja constancia del documento de origen de una nota, en la nota y en la factura original."""
        self.ensure_one()
        origin = self.origin_id
        if origin.move_id:
            move.message_post(body=self.env._(
                'Referencia a %(doc)s del proveedor: factura %(move)s.',
                doc='%s %s' % (origin.document_type_name, origin.folio), move=origin.move_id.display_name,
            ))
            origin.move_id.message_post(body=self.env._(
                'El proveedor emitió %(note)s sobre este documento: %(move)s.',
                note='%s %s' % (self.document_type_name, self.folio), move=move.display_name,
            ))
        elif origin:
            move.message_post(body=self.env._(
                'Referencia a %s del proveedor, que aún no tiene factura de proveedor.',
                '%s %s' % (origin.document_type_name, origin.folio),
            ))
        elif self.reference_ids:
            move.message_post(body=self.env._(
                'Referencias del documento: %s. No se encontró el documento de origen entre los recibidos.',
                ', '.join('%s %s' % (r.document_type_name or r.document_type, r.folio) for r in self.reference_ids),
            ))

    # ------------------------------------------------------------------
    # Guías de despacho: recepción asociada
    # ------------------------------------------------------------------
    def _tf_dte_cl_picking_candidates(self):
        """Recepciones posibles para una guía: por orden de compra referenciada o por proveedor y fecha."""
        self.ensure_one()
        Picking = self.env['stock.picking']
        base = [
            ('picking_type_code', '=', 'incoming'),
            ('company_id', '=', self.company_id.id),
            ('state', '!=', 'cancel'),
            ('tf_dte_cl_received_guide_ids', '=', False),
        ]
        orders = [r.folio for r in self.reference_ids if r.document_type == PURCHASE_ORDER_REFERENCE and r.folio]
        if orders and 'purchase_id' in Picking._fields:
            by_order = Picking.search(base + ['|', ('purchase_id.name', 'in', orders),
                                              ('purchase_id.partner_ref', 'in', orders)])
            if by_order:
                return by_order
        partners = self.env['res.partner'].search([('vat', '=', self.issuer_vat)])
        if not partners or not self.date:
            return Picking
        window = timedelta(days=PICKING_MATCH_DAYS)
        return Picking.search(base + [
            ('partner_id', 'child_of', partners.commercial_partner_id.ids),
            ('scheduled_date', '>=', fields.Datetime.to_datetime(self.date - window)),
            ('scheduled_date', '<=', fields.Datetime.to_datetime(self.date + window + timedelta(days=1))),
        ])

    def _tf_dte_cl_match_picking(self) -> None:
        """Asocia la guía con su recepción solo si hay una única candidata."""
        for received in self.filtered(lambda r: r.document_type == '52' and not r.picking_id):
            candidates = received._tf_dte_cl_picking_candidates()
            if len(candidates) == 1:
                received.picking_id = candidates
                received.message_post(body=self.env._('Asociada a la recepción %s.', candidates.name))
                candidates.message_post(body=self.env._(
                    'Guía de despacho del proveedor asociada: %s.', received.display_reference,
                ))

    def action_tf_dte_cl_match_picking(self):
        self._tf_dte_cl_match_picking()
        unmatched = self.filtered(lambda r: r.document_type == '52' and not r.picking_id)
        if unmatched:
            raise UserError(self.env._(
                'No se encontró una única recepción para %s. Selecciónela a mano en el campo Recepción.',
                ', '.join(unmatched.mapped('display_reference')),
            ))
        return True

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


def _parse_date(value: str):
    try:
        return date.fromisoformat((value or '')[:10])
    except ValueError:
        return False


class TfDteClReceivedReference(models.Model):
    _name = 'tf_dte_cl.received.reference'
    _description = 'Referencia de un documento recibido'
    _order = 'received_id, id'

    received_id = fields.Many2one('tf_dte_cl.received', required=True, ondelete='cascade', index=True)
    document_type = fields.Char(string='Tipo', help='Código del SII del documento referido (TpoDocRef).')
    document_type_name = fields.Char(string='Documento', compute='_compute_document_type_name')
    folio = fields.Char(string='Folio')
    date = fields.Date(string='Fecha')
    code = fields.Char(string='Código', help='1: anula, 2: corrige texto, 3: corrige montos (CodRef).')
    reason = fields.Char(string='Razón')

    @api.depends('document_type')
    def _compute_document_type_name(self):
        types = self.env['tf_dte_cl.document_type'].with_context(active_test=False)
        for reference in self:
            doc_type = types.search([('code', '=', reference.document_type)], limit=1)
            reference.document_type_name = doc_type.name or reference.document_type
