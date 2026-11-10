export const BRAND = {
  name: '序枢',
  englishName: 'Ontweft',
  descriptor: '企业本体平台',
  tagline: '让知识连接业务。',
} as const;

export function Brand({ inverse = false }: { inverse?: boolean }) {
  return <span className={`product-brand${inverse ? ' product-brand--inverse' : ''}`} role="img" aria-label={`${BRAND.name} ${BRAND.englishName}`}>
    <img className="product-brand__mark" src={`${import.meta.env.BASE_URL}brand/ontweft-mark${inverse ? '-inverse' : ''}.svg`} width="44" height="44" alt="" aria-hidden="true" />
    <span className="product-brand__wordmark" aria-hidden="true"><strong>{BRAND.name}</strong><span>{BRAND.englishName}</span></span>
  </span>;
}
