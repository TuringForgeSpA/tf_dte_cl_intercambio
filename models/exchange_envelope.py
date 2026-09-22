# -*- coding: utf-8 -*-
"""Sobres EnvioDTE recibidos de proveedores.

Ruta real: models/exchange_envelope.py

Los sobres llegan por correo a la casilla de intercambio (servidor de correo
entrante apuntando a este modelo). De cada sobre se registran sus documentos y
se responde automáticamente con la respuesta de recepción firmada, que es un
acuse técnico: confirma que el archivo llegó y es válido, sin decidir nada
comercial.
"""
from __future__ import annotations

import base64
import logging
import re

from lxml import etree
from psycopg2 import OperationalError

from odoo import api, fields, models

from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

_logger = logging.getLogger(__name__)

SII_NS = 'http://www.sii.cl/SiiDte'
ENVELOPE_STATES = [
    ('received', 'Recibido'),
    ('answered', 'Recepción respondida'),
    ('rejected', 'Rechazado'),
    ('error', 'Error'),
]


def _text(node, path: str, default: str = '') -> str:
    if node is None:
        return default
    found = node.find(path)
    return (found.text or '').strip() if found is not None and found.text else default


def _amount(node, path: str) -> int:
    value = _text(node, path)
    try:
        return int(round(float(value)))
    except ValueError:
        return 0


def parse_envelope(raw: bytes) -> dict:
    """Lee un EnvioDTE recibido y devuelve su carátula y sus documentos.

    No depende de Odoo. Lanza ``ValueError`` si el archivo no es un EnvioDTE.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as error:
        raise ValueError('El archivo no es un XML válido: %s' % error) from None
    # Se trabaja sin espacio de nombres para simplificar las rutas.
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.startswith('{%s}' % SII_NS):
            element.tag = etree.QName(element).localname
    etree.cleanup_namespaces(root)
    if root.tag != 'EnvioDTE':
        raise ValueError('El archivo no es un sobre EnvioDTE.')
    set_dte = root.find('SetDTE')
    caratula = set_dte.find('Caratula') if set_dte is not None else None
    if caratula is None:
        raise ValueError('El sobre no tiene carátula.')

    documents = []
    for dte in set_dte.findall('DTE'):
        header = dte.find('Documento/Encabezado')
        if header is None:
            continue
        id_doc, issuer, receiver, totals = (
            header.find('IdDoc'), header.find('Emisor'), header.find('Receptor'), header.find('Totales'),
        )
        lines = []
        for detail in dte.findall('Documento/Detalle'):
            quantity = float(_text(detail, 'QtyItem') or 1)
            lines.append({
                'name': _text(detail, 'NmbItem'),
                'description': _text(detail, 'DscItem'),
                'quantity': quantity,
                'price_unit': float(_text(detail, 'PrcItem') or 0),
                'amount': _amount(detail, 'MontoItem'),
                'is_exempt': bool(_text(detail, 'IndExe')),
            })
        documents.append({
            'document_type': _text(id_doc, 'TipoDTE'),
            'folio': _text(id_doc, 'Folio'),
            'date': _text(id_doc, 'FchEmis'),
            'issuer_vat': normalize_rut(_text(issuer, 'RUTEmisor')),
            'issuer_name': _text(issuer, 'RznSoc'),
            'issuer_activity': _text(issuer, 'GiroEmis'),
            'issuer_street': _text(issuer, 'DirOrigen'),
            'issuer_city': _text(issuer, 'CiudadOrigen'),
            'issuer_email': _text(issuer, 'CorreoEmisor'),
            'receiver_vat': normalize_rut(_text(receiver, 'RUTRecep')),
            'amount_net': _amount(totals, 'MntNeto'),
            'amount_exempt': _amount(totals, 'MntExe'),
            'amount_tax': _amount(totals, 'IVA'),
            'amount_total': _amount(totals, 'MntTotal'),
            'xml': etree.tostring(dte, encoding='ISO-8859-1').decode('ISO-8859-1'),
            'lines': lines,
        })
    if not documents:
        raise ValueError('El sobre no contiene documentos.')
    return {
        'issuer_vat': normalize_rut(_text(caratula, 'RutEmisor')),
        'receiver_vat': normalize_rut(_text(caratula, 'RutReceptor')),
        'set_id': set_dte.get('ID') or '',
        'documents': documents,
    }


class TfDteClExchangeEnvelope(models.Model):
    _name = 'tf_dte_cl.exchange.envelope'
    _description = 'Sobre DTE recibido'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string='Archivo', readonly=True)
    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True, index=True,
        default=lambda self: self.env.company,
    )
    partner_id = fields.Many2one('res.partner', string='Emisor', readonly=True)
    issuer_vat = fields.Char(string='RUT emisor', readonly=True, index=True)
    set_id = fields.Char(string='ID del sobre', readonly=True)
    state = fields.Selection(ENVELOPE_STATES, string='Estado', default='received', readonly=True)
    xml_file = fields.Binary(string='XML recibido', attachment=True, readonly=True, copy=False)
    xml_filename = fields.Char(readonly=True)
    response_file = fields.Binary(string='Respuesta enviada', attachment=True, readonly=True, copy=False)
    response_filename = fields.Char(readonly=True)
    response_date = fields.Datetime(string='Respondido el', readonly=True)
    response_glosa = fields.Char(string='Glosa de recepción', readonly=True)
    error = fields.Text(string='Mensaje', readonly=True)
    received_ids = fields.One2many('tf_dte_cl.received', 'envelope_id', string='Documentos')
    received_count = fields.Integer(compute='_compute_received_count')

    @api.depends('received_ids')
    def _compute_received_count(self):
        for envelope in self:
            envelope.received_count = len(envelope.received_ids)

    # ------------------------------------------------------------------
    # Entrada por correo
    # ------------------------------------------------------------------
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        """Crea un sobre por cada XML adjunto del correo recibido."""
        attachments = msg_dict.get('attachments') or []
        envelopes = self.browse()
        for attachment in attachments:
            filename = getattr(attachment, 'fname', '') or ''
            content = getattr(attachment, 'content', b'')
            if not filename.lower().endswith('.xml'):
                continue
            if isinstance(content, str):
                content = content.encode('ISO-8859-1', errors='replace')
            envelopes |= self._tf_dte_cl_register(content, filename, custom_values)
        if not envelopes:
            # Correo sin XML: se registra igual para que quede rastro.
            envelopes = super().message_new(msg_dict, custom_values)
            envelopes.write({'state': 'error', 'error': self.env._('El correo no traía un XML de DTE.')})
        return envelopes[:1]

    @api.model
    def _tf_dte_cl_register(self, content: bytes, filename: str, custom_values=None):
        """Registra un sobre recibido y sus documentos."""
        values = dict(custom_values or {}, name=filename, xml_filename=filename,
                      xml_file=base64.b64encode(content))
        try:
            data = parse_envelope(content)
        except ValueError as error:
            return self.create(dict(values, state='error', error=str(error)))
        company = self.env['res.company'].search([('vat', '=', data['receiver_vat'])], limit=1)
        if not company:
            company = self.env.company
        if normalize_rut(company.vat) != data['receiver_vat']:
            return self.create(dict(
                values, company_id=company.id, state='rejected', issuer_vat=data['issuer_vat'],
                error=self.env._('El sobre está dirigido al RUT %s, que no corresponde a esta compañía.',
                                 data['receiver_vat']),
            ))
        partner = self.env['res.partner'].search([('vat', '=', data['issuer_vat'])], limit=1)
        envelope = self.create(dict(
            values, company_id=company.id, issuer_vat=data['issuer_vat'],
            set_id=data['set_id'], partner_id=partner.id or False,
        ))
        envelope.received_ids = [
            fields.Command.create(self.env['tf_dte_cl.received']._tf_dte_cl_values(document, envelope))
            for document in data['documents']
        ]
        return envelope

    # ------------------------------------------------------------------
    # Respuesta de recepción
    # ------------------------------------------------------------------
    def _tf_dte_cl_response_payload(self) -> dict:
        self.ensure_one()
        company = self.company_id
        contact = self.env.user
        return dict(
            self.env['tf_dte_cl.received']._tf_dte_cl_base_payload(company),
            Recepciones=[{
                'xml_envio': self.xml_file,
                'xml_nombre': self.xml_filename or self.name,
                'IdRespuesta': self.id,
                'NmbContacto': contact.name,
                'MailContacto': company.tf_dte_cl_dte_email or company.email,
                'FonoContacto': company.phone,
            }],
        )

    def action_tf_dte_cl_answer_reception(self):
        for envelope in self.filtered(lambda e: e.state == 'received'):
            envelope._tf_dte_cl_answer_reception()
        return True

    def _tf_dte_cl_answer_reception(self):
        self.ensure_one()
        result = self.env['tf_dte_cl.sii.client'].tf_dte_cl_reception_response(
            self._tf_dte_cl_response_payload()
        )
        if not result.get('ok'):
            self.error = result.get('message')
            return False
        filename = result['filename']
        self.write({
            'response_file': base64.b64encode(result['xml'].encode('ISO-8859-1', errors='xmlcharrefreplace')),
            'response_filename': filename,
            'response_date': fields.Datetime.now(),
            'response_glosa': result.get('glosa'),
            'state': 'answered',
            'error': False,
        })
        self._tf_dte_cl_send_response(filename)
        return True

    def _tf_dte_cl_send_response(self, filename: str) -> None:
        """Envía la respuesta firmada al correo de intercambio del emisor."""
        self.ensure_one()
        email = (self.partner_id.commercial_partner_id.tf_dte_cl_dte_email
                 or self.received_ids[:1].issuer_email or self.partner_id.email)
        if not email:
            self.message_post(body=self.env._(
                'No se pudo enviar la respuesta de recepción: el emisor no tiene correo de intercambio.'
            ))
            return
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'datas': self.response_file,
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/xml',
        })
        self.env['mail.mail'].sudo().create({
            'subject': 'Recepción de envío %s' % (self.set_id or self.name),
            'email_from': self.company_id.tf_dte_cl_dte_email or self.company_id.email,
            'email_to': email,
            'body_html': self.env._(
                '<p>Se adjunta la respuesta de recepción del envío %(name)s.</p>'
                '<p>%(glosa)s</p>', name=self.name, glosa=self.response_glosa or '',
            ),
            'attachment_ids': [fields.Command.link(attachment.id)],
            'auto_delete': False,
        }).send()
        self.message_post(body=self.env._('Respuesta de recepción enviada a %s.', email))

    def _tf_dte_cl_lock_skip(self):
        """Filtra los sobres que otro proceso tiene bloqueados."""
        if not self:
            return self
        self.flush_recordset()
        self.env.cr.execute(
            'SELECT id FROM tf_dte_cl_exchange_envelope WHERE id IN %s FOR UPDATE SKIP LOCKED',
            [tuple(self.ids)],
        )
        return self.browse(row[0] for row in self.env.cr.fetchall())

    @api.model
    def _cron_tf_dte_cl_answer_receptions(self, limit=50):
        """Responde la recepción de los sobres pendientes.

        Cada sobre se bloquea antes de responder: el correo de respuesta no se
        puede deshacer con un rollback.
        """
        envelopes = self.search([('state', '=', 'received')], order='id', limit=limit)
        for envelope_id in envelopes.ids:
            envelope = self.browse(envelope_id)._tf_dte_cl_lock_skip()
            if not envelope:
                continue
            envelope.invalidate_recordset()
            if envelope.state != 'received':
                continue
            try:
                envelope._tf_dte_cl_answer_reception()
            except OperationalError:
                self.env.cr.rollback()
                _logger.warning('Respuesta de recepción pospuesta por concurrencia: %s', envelope_id)
                break
            except Exception as error:  # noqa: BLE001 - un sobre no debe detener el lote
                self.env.cr.rollback()
                _logger.exception('Error al responder la recepción del sobre %s', envelope_id)
                self.browse(envelope_id).write({'error': str(error)})
            if not self.env.registry.in_test_mode():
                self.env.cr.commit()

    def action_open_received(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Documentos recibidos'),
            'res_model': 'tf_dte_cl.received',
            'view_mode': 'list,form',
            'domain': [('envelope_id', '=', self.id)],
        }
