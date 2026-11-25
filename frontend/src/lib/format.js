// Display helpers shared by views.

export const CATEGORIES = ['restaurant', 'cafe', 'bar', 'temple', 'attraction', 'shop', 'hotel', 'other']

// One distinct colour per category; used for map markers and list dots.
export const CATEGORY_COLORS = {
  restaurant: '#e8590c',
  cafe: '#8d5524',
  bar: '#7048e8',
  temple: '#c2255c',
  attraction: '#2f9e44',
  shop: '#f08c00',
  hotel: '#1c7ed6',
  other: '#868e96',
}

export function timestamp(ms) {
  const s = Math.floor((ms ?? 0) / 1000)
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export function bytes(n) {
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

export function duration(ms) {
  if (ms == null) return '–'
  const s = Math.round(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

// Lowercase without diacritics, so "pho thin" finds "Phở Thìn".
export function fold(text) {
  return (text ?? '')
    .normalize('NFD')
    .replace(/[̀-ͯ]/g, '')
    .replace(/[đĐ]/g, 'd')
    .toLowerCase()
}
