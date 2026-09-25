# -*- coding: utf-8 -*-
"""Datos de prueba para tf_dte_cl_intercambio.

Ruta real: tests/common.py

Reutiliza la base de tf_dte_cl (compañía emisora, certificado, CAF, cliente y
simulación del SII). Los XML de proveedores se generan con la estructura de los
reales, pero con RUT, razones sociales y montos inventados.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import patch

from odoo import Command, fields

from odoo.addons.tf_dte_cl.tests.common import COMPANY_RUT, TfDteClCommon, make_rut, round_half_up

SUPPLIER_RUT = make_rut('77000001')
OTHER_SUPPLIER_RUT = make_rut('77000002')
SII_NS = 'http://www.sii.cl/SiiDte'


def dte_xml(folio: int, emission: date, issuer: str = SUPPLIER_RUT, receiver: str = COMPANY_RUT,
            payment: str | None = '2', lines=((1, 8803, False),), discount: int = 0,
            doc_type: str = '33', namespace: bool = False, references=()) -> str:
    """DTE de proveedor. ``lines``: (cantidad, precio, exento). ``discount``: descuento global en $.
    ``references``: (tipo, folio, fecha, código, razón)."""
    net = sum(round_half_up(q * p) for q, p, exempt in lines if not exempt) - discount
    exempt_total = sum(round_half_up(q * p) for q, p, exempt in lines if exempt)
    iva = round_half_up(net * 0.19)
    details = ''.join(
        '<Detalle><NroLinDet>%d</NroLinDet>%s<NmbItem>Servicio %d</NmbItem><DscItem>Detalle %d</DscItem>'
        '<QtyItem>%s</QtyItem><PrcItem>%s</PrcItem><MontoItem>%d</MontoItem></Detalle>'
        % (i, '<IndExe>1</IndExe>' if exempt else '', i, i, q, p, round_half_up(q * p))
        for i, (q, p, exempt) in enumerate(lines, start=1)
    )
    global_discount = (
        '<DscRcgGlobal><NroLinDR>1</NroLinDR><TpoMov>D</TpoMov><GlosaDR>Descuento de prueba</GlosaDR>'
        '<TpoValor>$</TpoValor><ValorDR>%d</ValorDR></DscRcgGlobal>' % discount
    ) if discount else ''
    payment_node = '<FmaPago>%s</FmaPago>' % payment if payment else ''
    reference_nodes = ''.join(
        '<Referencia><NroLinRef>%d</NroLinRef><TpoDocRef>%s</TpoDocRef><FolioRef>%s</FolioRef>'
        '<FchRef>%s</FchRef>%s<RazonRef>%s</RazonRef></Referencia>'
        % (i, ref_type, ref_folio, ref_date, '<CodRef>%s</CodRef>' % ref_code if ref_code else '', ref_reason)
        for i, (ref_type, ref_folio, ref_date, ref_code, ref_reason) in enumerate(references, start=1)
    )
    return (
        '<DTE%s version="1.0"><Documento ID="T%sF%s"><Encabezado>'
        '<IdDoc><TipoDTE>%s</TipoDTE><Folio>%s</Folio><FchEmis>%s</FchEmis>%s</IdDoc>'
        '<Emisor><RUTEmisor>%s</RUTEmisor><RznSoc>Proveedor de Prueba SpA</RznSoc>'
        '<GiroEmis>Servicios de prueba</GiroEmis><CorreoEmisor>dte@proveedor.cl</CorreoEmisor></Emisor>'
        '<Receptor><RUTRecep>%s</RUTRecep><RznSocRecep>Empresa de Prueba SpA</RznSocRecep></Receptor>'
        '<Totales><MntNeto>%d</MntNeto><MntExe>%d</MntExe><TasaIVA>19</TasaIVA><IVA>%d</IVA>'
        '<MntTotal>%d</MntTotal></Totales></Encabezado>%s%s%s</Documento></DTE>'
    ) % (
        ' xmlns="%s"' % SII_NS if namespace else '', doc_type, folio, doc_type, folio, emission.isoformat(),
        payment_node, issuer, receiver, net, exempt_total, iva, net + exempt_total + iva,
        details, global_discount, reference_nodes,
    )


def envio_xml(*dtes: str, issuer: str = SUPPLIER_RUT, receiver: str = COMPANY_RUT) -> bytes:
    """Sobre EnvioDTE estándar, con espacio de nombres del SII y codificación ISO-8859-1."""
    xml = (
        '<?xml version="1.0" encoding="ISO-8859-1"?>'
        '<EnvioDTE xmlns="%s" version="1.0"><SetDTE ID="SetPrueba"><Caratula version="1.0">'
        '<RutEmisor>%s</RutEmisor><RutEnvia>%s</RutEnvia><RutReceptor>%s</RutReceptor>'
        '<FchResol>2014-01-01</FchResol><NroResol>0</NroResol><TmstFirmaEnv>2026-01-01T10:00:00</TmstFirmaEnv>'
        '</Caratula>%s</SetDTE></EnvioDTE>'
    ) % (SII_NS, issuer, issuer, receiver, ''.join(dtes))
    return xml.encode('ISO-8859-1')


def wrapped_xml(dte: str) -> bytes:
    """DTE dentro del envoltorio propio de un proveedor de facturación, sin carátula (tipo Acepta)."""
    return ('<?xml version="1.0" encoding="ISO-8859-1"?><Document><Content>%s</Content></Document>'
            % dte).encode('ISO-8859-1')


def response_xml(folio: int, state: str = '0', issuer: str = COMPANY_RUT) -> bytes:
    """Respuesta comercial de un cliente (RespuestaDTE) sobre un DTE emitido por la compañía."""
    xml = (
        '<RespuestaDTE xmlns="%s" version="1.0"><Resultado ID="Resp"><Caratula version="1.0">'
        '<RutResponde>%s</RutResponde><RutRecibe>%s</RutRecibe></Caratula>'
        '<ResultadoDTE><TipoDTE>33</TipoDTE><Folio>%s</Folio><FchEmis>2026-01-01</FchEmis>'
        '<RUTEmisor>%s</RUTEmisor><RUTRecep>%s</RUTRecep><MntTotal>23800</MntTotal><CodEnvio>1</CodEnvio>'
        '<EstadoDTE>%s</EstadoDTE><EstadoDTEGlosa>Glosa de prueba</EstadoDTEGlosa></ResultadoDTE>'
        '</Resultado></RespuestaDTE>'
    ) % (SII_NS, OTHER_SUPPLIER_RUT, issuer, folio, issuer, OTHER_SUPPLIER_RUT, state)
    return xml.encode()


class IntercambioCommon(TfDteClCommon):
    """Base de tf_dte_cl más impuesto de compra, producto genérico y responsable."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.tax_iva_purchase = env['account.tax'].create({
            'name': 'IVA 19% compra (prueba)',
            'amount': 19.0,
            'amount_type': 'percent',
            'type_tax_use': 'purchase',
            'tax_group_id': cls.tax_group_iva.id,
            'company_id': cls.company.id,
            'country_id': cls.chile.id,
        })
        cls.responsible = env['res.users'].create({
            'name': 'Responsable DTE de prueba',
            'login': 'responsable_dte_prueba',
            'email': 'responsable@prueba.cl',
            'company_id': cls.company.id,
            'company_ids': [Command.set(cls.company.ids)],
            'groups_id': [Command.set([env.ref('account.group_account_invoice').id,
                                       env.ref('base.group_user').id])],
        })
        cls.company.write({
            'account_purchase_tax_id': cls.tax_iva_purchase.id,
            'tf_dte_cl_exchange_product_id': env.ref('tf_dte_cl_intercambio.product_exchange_generic').id,
            'tf_dte_cl_exchange_responsible_id': cls.responsible.id,
            'tf_dte_cl_exchange_alert_days': 2,
        })
        cls.today = fields.Date.context_today(env.user)

    @contextmanager
    def fake_sii(self):
        """Como en tf_dte_cl, y además sin fecha de recepción: la verificación también la consulta."""
        client_class = type(self.env['tf_dte_cl.sii.client'])
        not_found = {'ok': False, 'date': None, 'description': 'Documento no encontrado', 'transient': False}
        with super().fake_sii() as fake, \
                patch.object(client_class, 'tf_dte_cl_reception_date', lambda c, p: dict(not_found)):
            yield fake

    def register(self, content: bytes, filename: str = 'documento.xml'):
        return self.env['tf_dte_cl.exchange.envelope']._tf_dte_cl_register(content, filename)

    @contextmanager
    def fake_exchange(self, register_ok=True, events=(), register_code=0, reception_date=None):
        """Simula los servicios del SII propios del intercambio. Lleva la cuenta de las llamadas.

        ``reception_date``: fecha y hora local (Chile) que devuelve consultarFechaRecepcionSii.
        """
        calls = {'register': [], 'history': 0, 'reception': 0, 'reception_date': 0}

        def reception_date_call(client, payload):
            calls['reception_date'] += 1
            if reception_date:
                return {'ok': True, 'date': reception_date, 'description': '', 'transient': False}
            return {'ok': False, 'date': None, 'description': 'Documento no encontrado', 'transient': False}

        def register_claim(client, payload):
            calls['register'].append(payload['DTEClaim'][0].get('Claim'))
            return {'ok': register_ok, 'code': register_code, 'description': 'Respuesta simulada',
                    'events': [], 'transient': False}

        def claim_history(client, payload):
            calls['history'] += 1
            return {'ok': True, 'code': 15 if events else 16, 'description': 'Eventos simulados',
                    'events': [{'code': code, 'description': code, 'rut': '1-9', 'date': '2026-01-01'}
                               for code in events], 'transient': False}

        def reception(client, payload):
            calls['reception'] += 1
            return {'ok': True, 'xml': '<RespuestaDTE/>', 'filename': 'respuesta.xml', 'state': 0,
                    'glosa': 'Envío Ok', 'message': ''}

        client_class = type(self.env['tf_dte_cl.sii.client'])
        with patch.object(client_class, 'tf_dte_cl_register_claim', register_claim), \
                patch.object(client_class, 'tf_dte_cl_claim_history', claim_history), \
                patch.object(client_class, 'tf_dte_cl_reception_response', reception), \
                patch.object(client_class, 'tf_dte_cl_reception_date', reception_date_call):
            yield calls

    def credit_invoice_accepted(self):
        """Factura a crédito emitida y aceptada por el SII (con el SII simulado)."""
        term = self.env.ref('account.account_payment_term_30days', raise_if_not_found=False)
        if not term:
            term = self.env['account.payment.term'].create({
                'name': '30 días (prueba)',
                'line_ids': [Command.create({'value': 'percent', 'value_amount': 100, 'nb_days': 30})],
            })
        with self.fake_sii():
            move = self.create_invoice(invoice_payment_term_id=term.id)
            move.action_post()
            move._tf_dte_cl_send()
            move._tf_dte_cl_query()
        self.assertEqual(move.tf_dte_cl_state, 'accepted')
        self.assertGreater(move.invoice_date_due, move.tf_dte_cl_emission_date)
        return move
