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
from odoo.exceptions import UserError

from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut

_logger = logging.getLogger(__name__)

SII_NS = 'http://www.sii.cl/SiiDte'
ENVELOPE_STATES = [
    ('received', 'Recibido'),
    ('answered', 'Recepción respondida'),
    ('no_reception', 'Sin respuesta de recepción'),
    ('client_response', 'Respuesta de un cliente'),
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


def _global_adjustments(document, lines: list[dict]) -> list[dict]:
    """Descuentos y recargos globales (DscRcgGlobal) como líneas adicionales.

    TpoMov D = descuento (resta), R = recargo (suma). TpoValor $ = monto,
    % = porcentaje sobre las líneas afectas (o exentas si IndExeDR = 1).
    IndExeDR = 2 (no facturable) no afecta los totales y se omite.
    """
    adjustments = []
    for node in document.findall('DscRcgGlobal'):
        exempt_flag = _text(node, 'IndExeDR')
        if exempt_flag == '2':
            continue
        is_exempt = exempt_flag == '1'
        value = float(_text(node, 'ValorDR') or 0)
        if _text(node, 'TpoValor') == '%':
            base = sum(line['amount'] for line in lines if line['is_exempt'] == is_exempt)
            value = base * value / 100.0
        amount = int(round(value))
        if _text(node, 'TpoMov') == 'D':
            amount = -amount
        if not amount:
            continue
        label = _text(node, 'GlosaDR') or ('Descuento global' if amount < 0 else 'Recargo global')
        adjustments.append({
            'name': label, 'description': '', 'quantity': 1.0,
            'price_unit': float(amount), 'amount': amount, 'is_exempt': is_exempt,
        })
    return adjustments


def parse_envelope(raw: bytes) -> dict:
    """Lee un archivo con uno o más DTE y devuelve su carátula (si la hay) y sus documentos.

    Acepta el sobre EnvioDTE estándar, un DTE suelto o un envoltorio de un
    proveedor de facturación (por ejemplo, el <Document> de Acepta). Sin
    carátula no se puede generar la respuesta de recepción, pero los
    documentos se registran igual. No depende de Odoo; lanza ``ValueError``
    si el archivo no contiene ningún DTE.
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

    dtes = [root] if root.tag == 'DTE' else root.findall('.//DTE')
    if not dtes:
        raise ValueError('El archivo no contiene ningún DTE.')
    caratula = root.find('SetDTE/Caratula') if root.tag == 'EnvioDTE' else None
    set_dte = root.find('SetDTE') if root.tag == 'EnvioDTE' else None

    documents = []
    for dte in dtes:
        document = dte.find('Documento')
        header = document.find('Encabezado') if document is not None else None
        if header is None:
            continue
        id_doc, issuer, receiver, totals = (
            header.find('IdDoc'), header.find('Emisor'), header.find('Receptor'), header.find('Totales'),
        )
        lines = []
        for detail in document.findall('Detalle'):
            quantity = float(_text(detail, 'QtyItem') or 1)
            lines.append({
                'name': _text(detail, 'NmbItem'),
                'description': _text(detail, 'DscItem'),
                'quantity': quantity,
                'price_unit': float(_text(detail, 'PrcItem') or 0),
                'amount': _amount(detail, 'MontoItem'),
                'is_exempt': bool(_text(detail, 'IndExe')),
            })
        lines += _global_adjustments(document, lines)
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
        raise ValueError('El archivo no contiene documentos legibles.')
    first = documents[0]
    return {
        'has_caratula': caratula is not None,
        'issuer_vat': normalize_rut(_text(caratula, 'RutEmisor')) if caratula is not None else first['issuer_vat'],
        'receiver_vat': normalize_rut(_text(caratula, 'RutReceptor')) if caratula is not None else first['receiver_vat'],
        'set_id': (set_dte.get('ID') or '') if set_dte is not None else '',
        'documents': documents,
    }


# Estados de la respuesta comercial (RespuestaDTE/ResultadoDTE/EstadoDTE).
COMMERCIAL_STATES = {'0': 'Aceptado', '1': 'Aceptado con discrepancias', '2': 'Rechazado'}
RESPONSE_ROOTS = ('RespuestaDTE', 'EnvioRecibos')


def _strip_namespaces(root) -> None:
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.startswith('{'):
            element.tag = etree.QName(element).localname
    etree.cleanup_namespaces(root)


def parse_response(raw: bytes) -> dict | None:
    """Lee una respuesta de intercambio enviada por un cliente.

    Devuelve ``None`` si el archivo no es una respuesta (RespuestaDTE o
    EnvioRecibos). No depende de Odoo.
    """
    try:
        root = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
    except etree.XMLSyntaxError:
        return None
    _strip_namespaces(root)
    if root.tag not in RESPONSE_ROOTS:
        return None
    items = []
    if root.tag == 'EnvioRecibos':
        for receipt in root.iter('DocumentoRecibo'):
            items.append({
                'kind': 'goods', 'document_type': _text(receipt, 'TipoDoc'), 'folio': _text(receipt, 'Folio'),
                'issuer_vat': normalize_rut(_text(receipt, 'RUTEmisor')),
                'summary': 'Acuse de recibo de mercaderías o servicios (recinto: %s)' % (
                    _text(receipt, 'Recinto') or '-'),
            })
    else:
        for result in root.iter('ResultadoDTE'):
            state = _text(result, 'EstadoDTE')
            items.append({
                'kind': 'commercial', 'document_type': _text(result, 'TipoDTE'), 'folio': _text(result, 'Folio'),
                'issuer_vat': normalize_rut(_text(result, 'RUTEmisor')),
                'summary': 'Respuesta comercial: %s. %s' % (
                    COMMERCIAL_STATES.get(state, state), _text(result, 'EstadoDTEGlosa')),
            })
        for reception in root.iter('RecepcionDTE'):
            items.append({
                'kind': 'reception', 'document_type': _text(reception, 'TipoDTE'),
                'folio': _text(reception, 'Folio'), 'issuer_vat': normalize_rut(_text(reception, 'RUTEmisor')),
                'summary': 'Recepción del envío: %s' % (_text(reception, 'RecepDTEGlosa') or '-'),
            })
    return {'root': root.tag, 'items': items}


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
    response_summary = fields.Text(string='Contenido de la respuesta', readonly=True)
    response_move_ids = fields.Many2many('account.move', string='Facturas referidas', readonly=True)
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
        response = parse_response(content)
        if response is not None:
            return self._tf_dte_cl_register_client_response(response, values)
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
            # Sin carátula (DTE suelto o envoltorio de un proveedor) no se puede
            # generar la respuesta de recepción: cita el ID y la firma del sobre.
            state='received' if data['has_caratula'] else 'no_reception',
        ))
        duplicates = self.env['tf_dte_cl.received'].search([
            ('company_id', '=', company.id),
            ('issuer_vat', 'in', [d['issuer_vat'] for d in data['documents']]),
        ])
        known = {(r.issuer_vat, r.document_type, r.folio) for r in duplicates}
        new_documents = [
            d for d in data['documents']
            if (d['issuer_vat'], d['document_type'], int(d['folio'] or 0)) not in known
        ]
        if len(new_documents) < len(data['documents']):
            envelope.message_post(body=self.env._(
                'Se omitieron %s documento(s) que ya estaban registrados.',
                len(data['documents']) - len(new_documents),
            ))
        if not new_documents and envelope.state == 'received':
            # Reenvío de documentos ya registrados: no se responde otra vez la recepción.
            envelope.state = 'no_reception'
        data['documents'] = new_documents
        envelope.received_ids = [
            fields.Command.create(self.env['tf_dte_cl.received']._tf_dte_cl_values(document, envelope))
            for document in data['documents']
        ]
        return envelope

    @api.model
    def _tf_dte_cl_register_client_response(self, response: dict, values: dict):
        """Registra una respuesta de un cliente y la informa en las facturas referidas."""
        moves = self.env['account.move']
        lines = []
        for item in response['items']:
            company = self.env['res.company'].search([('vat', '=', item['issuer_vat'])], limit=1)
            move = self.env['account.move'].search([
                ('company_id', '=', company.id),
                ('tf_dte_cl_document_type', '=', item['document_type']),
                ('tf_dte_cl_folio', '=', int(item['folio'] or 0)),
            ], limit=1) if company else self.env['account.move']
            lines.append('T%sF%s: %s' % (item['document_type'], item['folio'], item['summary']))
            if move:
                moves |= move
                previous = move.tf_dte_cl_client_mail_response
                move.tf_dte_cl_client_mail_response = '\n'.join(filter(None, [previous, item['summary']]))
                move.message_post(body=self.env._('Respuesta del cliente por correo: %s', item['summary']))
        envelope = self.create(dict(
            values, state='client_response', company_id=(moves[:1].company_id or self.env.company).id,
            response_summary='\n'.join(lines) or self.env._('La respuesta no refiere documentos.'),
            response_move_ids=[fields.Command.set(moves.ids)],
        ))
        if len(moves) < len(response['items']):
            envelope.message_post(body=self.env._(
                'Algunos documentos de la respuesta no corresponden a facturas emitidas en Odoo.'
            ))
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


class TfDteClReceivedUpload(models.TransientModel):
    _name = 'tf_dte_cl.received.upload'
    _description = 'Carga manual de un XML de proveedor'

    xml_file = fields.Binary(string='Archivo XML', attachment=False)
    xml_filename = fields.Char(string='Nombre del archivo')

    def action_upload(self):
        self.ensure_one()
        if not self.xml_file:
            raise UserError(self.env._('Adjunte el archivo XML del documento.'))
        envelope = self.env['tf_dte_cl.exchange.envelope']._tf_dte_cl_register(
            base64.b64decode(self.xml_file), self.xml_filename or 'documento.xml',
        )
        if envelope.state == 'error' or envelope.state == 'rejected':
            raise UserError(envelope.error)
        if not envelope.received_ids:
            raise UserError(self.env._('Los documentos del archivo ya estaban registrados.'))
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Documentos recibidos'),
            'res_model': 'tf_dte_cl.received',
            'view_mode': 'list,form',
            'domain': [('envelope_id', '=', envelope.id)],
        }
