# -*- coding: utf-8 -*-
"""Funciones de intercambio de facturacion_electronica.

Ruta real: models/sii_client.py

Extiende el cliente de tf_dte_cl; la librería se importa solo allí.
La librería genera los XML de respuesta firmados, pero no envía correos: el
correo lo maneja Odoo.
"""
import logging
from datetime import datetime

from odoo import api, models

from odoo.addons.tf_dte_cl.models.res_partner import normalize_rut
from odoo.addons.tf_dte_cl.models.sii_client import clean_payload, fe, short_message

try:
    # Solo para consultarFechaRecepcionSii, que la librería no expone como función.
    from facturacion_electronica.conexion import Conexion, claim_url
    from facturacion_electronica.emisor import Emisor
    from facturacion_electronica.firma import Firma
except ImportError:  # la librería falta: tf_dte_cl ya lo informa al usarla
    Conexion = Emisor = Firma = claim_url = None

_logger = logging.getLogger(__name__)


class TfDteClSiiClient(models.AbstractModel):
    _inherit = 'tf_dte_cl.sii.client'

    @api.model
    def tf_dte_cl_reception_response(self, payload: dict) -> dict:
        """Respuesta de recepción de un envío (``fe.recepcion_xml``).

        Devuelve {'ok', 'xml', 'filename', 'state', 'glosa', 'message'}.
        ``state`` 0 significa envío recibido conforme.
        """
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload)
        try:
            responses = fe.recepcion_xml(payload)
        except Exception as error:  # noqa: BLE001
            _logger.exception('facturacion_electronica.recepcion_xml falló')
            return {'ok': False, 'message': short_message(error)}
        if isinstance(responses, dict) and responses.get('error'):
            return {'ok': False, 'message': short_message(responses['error'])}
        response = responses[0] if isinstance(responses, list) and responses else None
        if not isinstance(response, dict) or not response.get('respuesta_xml'):
            return {'ok': False, 'message': self.env._('La librería no generó la respuesta de recepción.')}
        return {
            'ok': True,
            'xml': response['respuesta_xml'],
            'filename': response.get('nombre_xml') or 'recepcion_envio.xml',
            'state': response.get('EstadoRecepEnv'),
            'glosa': response.get('RecepEnvGlosa') or '',
            'message': '',
        }


# ---------------------------------------------------------------------------
# Aceptación y reclamo ante el SII
# Fuente: SII, "Web Service de Consulta y Registro de Aceptación/Reclamo a DTE
# recibido", v1.2 (2017-08-07).
# ---------------------------------------------------------------------------
CLAIM_ACTIONS = {
    'ACD': 'Acepta contenido del documento',
    'ERM': 'Otorga recibo de mercaderías o servicios',
    'RCD': 'Reclamo al contenido del documento',
    'RFP': 'Reclamo por falta parcial de mercaderías',
    'RFT': 'Reclamo por falta total de mercaderías',
}
CLAIM_DOCUMENT_TYPES = ('33', '34', '43')
CLAIM_OK = 0
CLAIM_ALREADY_REGISTERED = 7
CLAIM_RETRY = -1


def _field(obj, name):
    """Lee un campo de una respuesta de zeep (objeto o dict)."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    try:
        return obj[name]
    except (KeyError, TypeError, IndexError):
        return getattr(obj, name, None)


def normalize_claim_response(result) -> dict:
    """Normaliza la respuesta de set_dte_claim / get_dte_claim de la librería.

    Devuelve {'ok', 'code', 'description', 'events', 'transient'}:
    ``transient`` indica que conviene reintentar más tarde (sin token, error de
    red o código -1 del SII).
    """
    if not isinstance(result, dict):
        return {'ok': False, 'code': None, 'description': 'Respuesta inesperada de la librería.',
                'events': [], 'transient': True}
    if result.get('errores'):
        return {'ok': False, 'code': None, 'description': '; '.join(map(str, result['errores'])),
                'events': [], 'transient': True}
    response = result.get('respuesta')
    code = _field(response, 'codResp')
    try:
        code = int(code)
    except (TypeError, ValueError):
        code = None
    events = []
    for event in _field(response, 'listaEventosDoc') or []:
        events.append({
            'code': _field(event, 'codEvento'),
            'description': _field(event, 'descEvento'),
            'rut': '%s-%s' % (_field(event, 'rutResponsable') or '', _field(event, 'dvResponsable') or ''),
            'date': str(_field(event, 'fechaEvento') or ''),
        })
    return {
        'ok': code in (CLAIM_OK, CLAIM_ALREADY_REGISTERED, 15, 16),
        'code': code,
        'description': str(_field(response, 'descResp') or ''),
        'events': events,
        'transient': code in (None, CLAIM_RETRY),
    }


RECEPTION_DATE_FORMATS = ('%d-%m-%Y %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%d-%m-%Y %H:%M', '%d-%m-%Y', '%Y-%m-%d')


def parse_reception_date(value) -> datetime | None:
    """Fecha de recepción que devuelve consultarFechaRecepcionSii, o None si no es una fecha.

    El servicio responde texto: la fecha, o un mensaje si el documento no existe.
    """
    text = str(value or '').strip()
    for fmt in RECEPTION_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class TfDteClSiiClientClaims(models.AbstractModel):
    _inherit = 'tf_dte_cl.sii.client'

    @api.model
    def _tf_dte_cl_claim_call(self, function_name: str, payload: dict) -> dict:
        self._tf_dte_cl_check_library()
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload)
        try:
            responses = getattr(fe, function_name)(payload)
        except Exception as error:  # noqa: BLE001 - red o respuesta inesperada del SII
            _logger.exception('facturacion_electronica.%s falló', function_name)
            return {'ok': False, 'code': None, 'description': short_message(error),
                    'events': [], 'transient': True}
        result = next(iter(responses.values()), None) if isinstance(responses, dict) and responses else None
        return normalize_claim_response(result)

    @api.model
    def tf_dte_cl_register_claim(self, payload: dict) -> dict:
        """Registra una aceptación, acuse de recibo o reclamo (ingresarAceptacionReclamoDoc)."""
        return self._tf_dte_cl_claim_call('ingreso_reclamo_documento', payload)

    @api.model
    def tf_dte_cl_reception_date(self, payload: dict) -> dict:
        """Fecha en que el SII recibió un documento (consultarFechaRecepcionSii).

        La librería 0.24.0 no incluye este método del servicio de registro de
        reclamos. Se llama con la misma conexión que usan sus funciones de reclamo
        (``Conexion._client`` y ``Conexion._call_with_retry``): revisar al cambiar
        de versión de la librería.
        """
        self._tf_dte_cl_check_library()
        if Conexion is None:
            return {'ok': False, 'date': None, 'description': 'Librería facturacion_electronica incompleta',
                    'transient': False}
        payload = clean_payload(payload)
        self._tf_dte_cl_check_payload(payload)
        document = payload['DTEClaim'][0]
        rut = normalize_rut(document['RUTEmisor'])
        try:
            connection = Conexion(Emisor(payload['Emisor']), Firma(payload['firma_electronica']))
            if not connection.token:
                return {'ok': False, 'date': None, 'description': 'Sin token del SII', 'transient': True}
            server = connection._client(claim_url[connection.Emisor.Modo] + '?wsdl', True)
            answer = connection._call_with_retry(
                lambda: server.service.consultarFechaRecepcionSii(
                    rut[:-2], rut[-1], str(document['TipoDTE']), str(document['Folio']),
                ),
                label='consultarFechaRecepcionSii',
            )
        except Exception as error:  # noqa: BLE001 - red o respuesta inesperada del SII
            _logger.exception('Consulta de fecha de recepción en el SII falló')
            return {'ok': False, 'date': None, 'description': short_message(error), 'transient': True}
        received = parse_reception_date(answer)
        if not received:
            return {'ok': False, 'date': None, 'description': str(answer or '')[:250], 'transient': False}
        return {'ok': True, 'date': received, 'description': '', 'transient': False}

    @api.model
    def tf_dte_cl_claim_history(self, payload: dict) -> dict:
        """Eventos registrados en el SII para un documento (listarEventosHistDoc)."""
        return self._tf_dte_cl_claim_call('consulta_reclamo_documento', payload)
