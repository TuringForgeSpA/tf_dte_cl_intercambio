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

from psycopg2 import OperationalError

from odoo import api, fields, models

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
