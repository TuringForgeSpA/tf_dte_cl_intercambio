# -*- coding: utf-8 -*-
"""Envío del DTE al receptor (intercambio).

Ruta real: models/account_move.py

Además del sobre que va al SII, se arma uno dirigido al cliente y se envía por
correo con el XML y el PDF. Solo se envía a clientes con correo de intercambio
y una vez que el SII aceptó el documento.
"""
from __future__ import annotations

import base64
import logging
from datetime import timedelta

from psycopg2 import OperationalError

from odoo import api, fields, models

from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

_logger = logging.getLogger(__name__)

EXCHANGE_STATES = [
    ('to_send', 'Por enviar'),
    ('sent', 'Enviado al receptor'),
    ('not_applicable', 'No aplica'),
    ('error', 'Error'),
]
SII_ACCEPTED_STATES = ('accepted', 'accepted_objections')


class AccountMove(models.Model):
    _inherit = 'account.move'

    tf_dte_cl_exchange_state = fields.Selection(
        EXCHANGE_STATES, string='Intercambio', readonly=True, copy=False, index=True,
    )
    tf_dte_cl_exchange_date = fields.Datetime(string='Enviado al receptor el', readonly=True, copy=False)
    tf_dte_cl_exchange_email = fields.Char(string='Correo de intercambio', readonly=True, copy=False)
    tf_dte_cl_exchange_error = fields.Text(string='Mensaje de intercambio', readonly=True, copy=False)

    def _tf_dte_cl_exchange_recipient(self) -> str:
        self.ensure_one()
        return self.partner_id.commercial_partner_id.tf_dte_cl_dte_email or ''

    def _tf_dte_cl_exchange_envelope(self) -> dict:
        """Sobre dirigido al receptor: igual al del SII, pero con el RUT del cliente."""
        self.ensure_one()
        client = self.env['tf_dte_cl.sii.client']
        envelope = self.tf_dte_cl_envelope_id.sudo()
        payload = dict(
            self._tf_dte_cl_base_payload(),
            ID='%sR' % envelope.name,
            filename='EnvioDTE_%s_receptor.xml' % self._tf_dte_cl_label(),
            RutReceptor=self.tf_dte_cl_receiver_rut,
            Documento=[{
                'TipoDTE': int(self.tf_dte_cl_document_type),
                'documentos': [{
                    'NroDTE': 1,
                    'Folio': self.tf_dte_cl_folio,
                    'sii_xml_request': base64.b64decode(self.tf_dte_cl_xml_file).decode('ISO-8859-1'),
                }],
            }],
        )
        return client.tf_dte_cl_build_envelope(payload)

    def _tf_dte_cl_exchange_send(self) -> str:
        self.ensure_one()
        if self.tf_dte_cl_exchange_state == 'sent':
            return 'sent'
        email = self._tf_dte_cl_exchange_recipient()
        if not email:
            self.write({'tf_dte_cl_exchange_state': 'not_applicable',
                        'tf_dte_cl_exchange_error': self.env._('El cliente no tiene correo de intercambio.')})
            return 'not_applicable'
        result = self._tf_dte_cl_exchange_envelope()
        if not result.ok:
            self.write({'tf_dte_cl_exchange_state': 'error', 'tf_dte_cl_exchange_error': result.message})
            return 'error'
        # El XML queda en la factura como respaldo del intercambio.
        attachments = self.env['ir.attachment'].create([{
            'name': result.filename,
            'datas': base64.b64encode(result.xml.encode('ISO-8859-1', errors='xmlcharrefreplace')),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/xml',
        }])
        # El PDF se genera con el reporte del módulo (el estándar guarda una copia
        # automática) y no se adjunta a la factura: un PDF adjunto haría que Odoo
        # abriera el visor de documentos y desplazara el chatter.
        pdf, _type = self.env['ir.actions.report']._render_qweb_pdf('tf_dte_cl.report_move_dte', self.ids)
        attachments |= self.env['ir.attachment'].create([{
            'name': 'DTE_%s.pdf' % self._tf_dte_cl_label(),
            'datas': base64.b64encode(pdf),
            'mimetype': 'application/pdf',
        }])
        template = self.env.ref('tf_dte_cl_intercambio.mail_template_exchange_dte', raise_if_not_found=False)
        body = template._render_field('body_html', self.ids)[self.id] if template else self.env._(
            '<p>Se adjunta el documento tributario electrónico.</p>')
        self.env['mail.mail'].sudo().create({
            'subject': '%s N° %s - %s' % (self._tf_dte_cl_document_name(), self.tf_dte_cl_folio,
                                          self.company_id.name),
            'email_from': self.company_id.tf_dte_cl_dte_email or self.company_id.email,
            'email_to': email,
            'body_html': body,
            'attachment_ids': [fields.Command.set(attachments.ids)],
            'auto_delete': False,
        }).send()
        self.write({
            'tf_dte_cl_exchange_state': 'sent',
            'tf_dte_cl_exchange_date': fields.Datetime.now(),
            'tf_dte_cl_exchange_email': email,
            'tf_dte_cl_exchange_error': False,
        })
        self.message_post(body=self.env._('DTE enviado al receptor (%s).', email))
        return 'sent'

    def action_tf_dte_cl_exchange_send(self):
        for move in self.filtered(lambda m: m.tf_dte_cl_state in SII_ACCEPTED_STATES
                                  and m.tf_dte_cl_exchange_state != 'sent'):
            move._tf_dte_cl_exchange_send()
        return True

    @api.model
    def _cron_tf_dte_cl_exchange_send(self, limit=50):
        """Envía los DTE aceptados a sus receptores.

        Cada factura se bloquea antes de procesarla: el correo no se puede
        deshacer con un rollback, así que dos procesos no deben tomar la misma.
        """
        moves = self.search([
            ('tf_dte_cl_state', 'in', list(SII_ACCEPTED_STATES)),
            ('tf_dte_cl_exchange_state', 'in', [False, 'to_send', 'error']),
            ('company_id.tf_dte_cl_exchange_auto_send', '=', True),
        ], order='id', limit=limit)
        for move_id in moves.ids:
            move = self.browse(move_id)._tf_dte_cl_lock_skip()
            if not move:
                continue  # otra sesión la está procesando
            move.invalidate_recordset()
            if move.tf_dte_cl_exchange_state == 'sent':
                continue
            try:
                move._tf_dte_cl_exchange_send()
            except OperationalError:
                # Transacción abortada (por ejemplo, por concurrencia): no se puede
                # escribir nada más; se reintenta en la próxima ejecución.
                self.env.cr.rollback()
                _logger.warning('Envío al receptor pospuesto por concurrencia: %s', move_id)
                break
            except Exception as error:  # noqa: BLE001 - una factura no debe detener el lote
                self.env.cr.rollback()
                _logger.exception('Error al enviar el DTE %s al receptor', move_id)
                self.browse(move_id).write({
                    'tf_dte_cl_exchange_state': 'error',
                    'tf_dte_cl_exchange_error': str(error),
                })
            if not self.env.registry.in_test_mode():
                self.env.cr.commit()


# ---------------------------------------------------------------------------
# Respuesta del cliente a los DTE emitidos
# ---------------------------------------------------------------------------
CLIENT_STATES = [
    ('pending', 'Pendiente'),
    ('accepted', 'Aceptado por el cliente'),
    ('receipt', 'Acuse de recibo otorgado'),
    ('claimed', 'Reclamado por el cliente'),
    ('presumed', 'Acuse de recibo presunto'),
    ('not_applicable', 'No aplica'),
]
CLIENT_CLAIM_CODES = ('RCD', 'RFP', 'RFT')
CLIENT_DOCUMENT_TYPES = ('33', '34')  # 43 está fuera del alcance de tf_dte_cl
CLIENT_DAYS = 8
# El plazo real corre desde la recepción en el SII, que puede ser posterior a la
# emisión: se sigue consultando unos días más antes de dar el acuse por presunto.
CLIENT_MARGIN_DAYS = 2
CLIENT_OPEN_STATES = (False, 'pending', 'accepted')


def client_state_from_events(codes: set, deadline_passed: bool) -> str:
    """Estado del cliente según los eventos registrados en el SII. No depende de Odoo."""
    if codes & set(CLIENT_CLAIM_CODES):
        return 'claimed'
    if 'ERM' in codes:
        return 'receipt'
    if 'ACD' in codes:
        return 'accepted'
    return 'presumed' if deadline_passed else 'pending'


class AccountMoveClientResponse(models.Model):
    _inherit = 'account.move'

    tf_dte_cl_client_state = fields.Selection(
        CLIENT_STATES, string='Respuesta del cliente', readonly=True, copy=False, index=True,
    )
    tf_dte_cl_client_claim = fields.Char(string='Reclamo del cliente', readonly=True, copy=False)
    tf_dte_cl_client_deadline = fields.Date(
        string='Plazo del cliente (estimado)', compute='_compute_tf_dte_cl_client_deadline', store=True,
        help='8 días corridos desde la recepción en el SII, estimados desde la fecha de emisión.',
    )
    tf_dte_cl_client_events = fields.Text(string='Eventos del cliente en el SII', readonly=True, copy=False)
    tf_dte_cl_client_last_query = fields.Datetime(string='Última consulta de eventos', readonly=True, copy=False)
    tf_dte_cl_client_message = fields.Char(string='Respuesta de la consulta', readonly=True, copy=False)
    tf_dte_cl_client_mail_response = fields.Text(
        string='Respuestas del cliente por correo', readonly=True, copy=False,
        help='Respuestas XML enviadas por el cliente a la casilla de intercambio. Son informativas: '
             'la aceptación o el reclamo con efecto legal es el registrado en el SII.',
    )

    def _tf_dte_cl_client_applicable(self) -> bool:
        """Facturas 33/34 a crédito aceptadas por el SII: las únicas que admiten eventos del cliente."""
        self.ensure_one()
        is_credit = bool(self.invoice_date_due and self.tf_dte_cl_emission_date
                         and self.invoice_date_due > self.tf_dte_cl_emission_date)
        return (self.move_type == 'out_invoice'
                and self.tf_dte_cl_document_type in CLIENT_DOCUMENT_TYPES
                and self.tf_dte_cl_state in SII_ACCEPTED_STATES
                and is_credit)

    @api.depends('tf_dte_cl_emission_date', 'tf_dte_cl_document_type', 'invoice_date_due', 'move_type')
    def _compute_tf_dte_cl_client_deadline(self):
        for move in self:
            applicable = (move.move_type == 'out_invoice'
                          and move.tf_dte_cl_document_type in CLIENT_DOCUMENT_TYPES
                          and move.tf_dte_cl_emission_date)
            move.tf_dte_cl_client_deadline = (
                applicable and move.tf_dte_cl_emission_date + timedelta(days=CLIENT_DAYS)
            ) or False

    def _tf_dte_cl_query_client_events(self) -> str:
        """Consulta en el SII los eventos del cliente sobre este DTE y actualiza su estado."""
        self.ensure_one()
        if not self._tf_dte_cl_client_applicable():
            self.tf_dte_cl_client_state = 'not_applicable'
            return 'not_applicable'
        payload = dict(self._tf_dte_cl_base_payload(), DTEClaim=[{
            'RUTEmisor': normalize_rut(self.company_id.vat),
            'TipoDTE': self.tf_dte_cl_document_type,
            'Folio': self.tf_dte_cl_folio,
        }])
        result = self.env['tf_dte_cl.sii.client'].tf_dte_cl_claim_history(payload)
        vals = {
            'tf_dte_cl_client_last_query': fields.Datetime.now(),
            'tf_dte_cl_client_message': '%s: %s' % (result.get('code'), result.get('description') or ''),
        }
        if not result['ok']:
            self.write(vals)
            return self.tf_dte_cl_client_state or 'pending'
        codes = {event['code'] for event in result['events']}
        today = fields.Date.context_today(self)
        deadline_passed = bool(
            self.tf_dte_cl_client_deadline
            and today > self.tf_dte_cl_client_deadline + timedelta(days=CLIENT_MARGIN_DAYS)
        )
        state = client_state_from_events(codes, deadline_passed)
        vals.update({
            'tf_dte_cl_client_state': state,
            'tf_dte_cl_client_events': '\n'.join(
                '%(date)s  %(code)s  %(description)s  (%(rut)s)' % event for event in result['events']
            ) or False,
        })
        previous = self.tf_dte_cl_client_state
        if state == 'claimed':
            vals['tf_dte_cl_client_claim'] = next(code for code in CLIENT_CLAIM_CODES if code in codes)
        self.write(vals)
        if state != previous:
            self._tf_dte_cl_notify_client_state(state)
        return state

    def _tf_dte_cl_notify_client_state(self, state: str) -> None:
        self.ensure_one()
        label = dict(CLIENT_STATES)[state]
        if state != 'claimed':
            self.message_post(body=self.env._('Respuesta del cliente en el SII: %s.', label))
            return
        from odoo.addons.tf_dte_cl_intercambio.models.sii_client import CLAIM_ACTIONS
        claim = CLAIM_ACTIONS.get(self.tf_dte_cl_client_claim, self.tf_dte_cl_client_claim)
        body = self.env._(
            'El cliente reclamó %(doc)s en el SII (%(claim)s). Corresponde revisar el caso y, '
            'si procede, emitir una nota de crédito.', doc=self._tf_dte_cl_label(), claim=claim,
        )
        self.message_post(body=body)
        self.activity_schedule(
            'mail.mail_activity_data_todo',
            summary=self.env._('Cliente reclamó el DTE %s', self._tf_dte_cl_label()),
            note=body,
            user_id=(self.invoice_user_id or self.create_uid).id,
        )

    def action_tf_dte_cl_query_client_events(self):
        for move in self:
            move._tf_dte_cl_query_client_events()
        return True

    @api.model
    def _cron_tf_dte_cl_client_events(self, limit=100):
        """Consulta los eventos del cliente mientras corre el plazo (más un margen)."""
        today = fields.Date.context_today(self)
        moves = self.search([
            ('move_type', '=', 'out_invoice'),
            ('tf_dte_cl_state', 'in', list(SII_ACCEPTED_STATES)),
            ('tf_dte_cl_document_type', 'in', list(CLIENT_DOCUMENT_TYPES)),
            ('tf_dte_cl_client_state', 'in', list(CLIENT_OPEN_STATES)),
            '|', ('tf_dte_cl_client_deadline', '=', False),
            ('tf_dte_cl_client_deadline', '>=', today - timedelta(days=CLIENT_MARGIN_DAYS + 1)),
        ], order='tf_dte_cl_client_last_query asc nulls first, id', limit=limit)
        for move_id in moves.ids:
            move = self.browse(move_id)
            try:
                with self.env.cr.savepoint():
                    move._tf_dte_cl_query_client_events()
            except OperationalError:
                self.env.cr.rollback()
                _logger.warning('Consulta de eventos del cliente pospuesta por concurrencia: %s', move_id)
                break
            except Exception:  # noqa: BLE001 - una factura no debe detener el lote
                self.env.cr.rollback()
                _logger.exception('Error al consultar los eventos del cliente de %s', move_id)
            if not self.env.registry.in_test_mode():
                self.env.cr.commit()
        # Pasado el plazo y el margen sin eventos, el acuse de recibo se presume.
        stale = self.search([
            ('tf_dte_cl_client_state', 'in', [False, 'pending']),
            ('tf_dte_cl_state', 'in', list(SII_ACCEPTED_STATES)),
            ('tf_dte_cl_client_deadline', '<', today - timedelta(days=CLIENT_MARGIN_DAYS + 1)),
        ], limit=limit)
        for move in stale:
            move._tf_dte_cl_query_client_events()
        if stale and not self.env.registry.in_test_mode():
            self.env.cr.commit()
