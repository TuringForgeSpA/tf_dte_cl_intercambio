# -*- coding: utf-8 -*-
"""Funciones de intercambio de facturacion_electronica.

Ruta real: models/sii_client.py

Extiende el cliente de tf_dte_cl; la librería se importa solo allí.
La librería genera los XML de respuesta firmados, pero no envía correos: el
correo lo maneja Odoo.
"""
import logging

from odoo import api, models

from odoo.addons.tf_dte_cl.models.sii_client import clean_payload, fe, short_message

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
