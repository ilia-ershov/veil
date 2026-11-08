// Domain of the forwarding addresses; must match MAIL_DOMAIN on the backend.
export const MAIL_DOMAIN = import.meta.env.VITE_MAIL_DOMAIN || 'veil.ershov.me'

// Same-origin /api by default (Vite proxies it in development, nginx in production).
export const API_URL = import.meta.env.VITE_API_URL || '/api'
