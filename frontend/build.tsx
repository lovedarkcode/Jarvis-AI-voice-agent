/** Compile real ChaiUI components to static HTML; app.js owns live behavior. */
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import parse, { attributesToProps, domToReact, Element } from 'html-react-parser'
import { readFileSync, writeFileSync, mkdirSync, copyFileSync, readdirSync } from 'node:fs'
import { Button } from './components/ui/button'
import { IconButton } from './components/ui/icon-button'
import { Input } from './components/ui/input'
import { Select } from './components/ui/select'
import { Textarea } from './components/ui/textarea'
import { Card } from './components/ui/card'
import { Chip } from './components/ui/chip'
import { Backdrop } from './components/backdrop'
import { Contrast } from 'lucide-react'

const options = { replace(node: any): any {
  if (!(node instanceof Element)) return
  const p: any = attributesToProps(node.attribs)
  const children = () => domToReact(node.children as any, options)
  if (node.name === 'svg' && p.className?.includes('theme-light')) return <Contrast className="icon" aria-hidden="true" />
  if (node.name === 'svg' && p.className?.includes('theme-dark')) return <></>
  if (node.name === 'main') return <main {...p} inert={p.inert !== undefined}>{children()}</main>
  if (node.name === 'button') {
    const solid = ['save-keys', 'send'].includes(p.id)
    const icon = p['aria-label'] && !p.className?.includes('theme') && !node.children.some((n: any) => n.type === 'text' && n.data.trim()) && p.id !== 'send'
    if (icon) return <IconButton {...p} variant="ghost">{children()}</IconButton>
    return <Button {...p} variant={solid ? 'solid' : ['record', 'new-chat'].includes(p.id) ? 'outline' : 'ghost'}
      size={p.id === 'send' ? 'icon' : 'md'} iconRight={null}>{children()}</Button>
  }
  if (node.name === 'input') {
    if (p.type === 'file') return
    if (p.type === 'checkbox') {
      // Native adapter: app.js uses .checked and onchange. Radix requires hydration.
      const { checked, ...rest } = p
      return <input {...rest} defaultChecked={checked} data-slot="checkbox" className="chai-checkbox" />
    }
    return <Input {...p} />
  }
  if (node.name === 'select') return <Select {...p}>{children()}</Select>
  if (node.name === 'textarea') return <Textarea {...p} defaultValue="" />
  if (node.name === 'div' && /(?:^| )(gate-card|composer|current-chat)(?: |$)/.test(p.className || '')) {
    return <Card {...p}>{children()}</Card>
  }
  if (p.id === 'state' || p.id === 'file-name') return <Chip {...p}>{children()}</Chip>
  if (node.name === 'label' && p.className === 'attach') {
    return <Button asChild variant="ghost"><label {...p} tabIndex={0} role="button">{children()}</label></Button>
  }
  if (node.name === 'dialog') {
    // ChaiUI panel tokens with native showModal focus trapping, Escape and inertness.
    return <dialog {...p} data-slot="dialog" className="chai-dialog" aria-label={p.id === 'audio-dialog' ? 'Audio settings' : 'Settings'}>{children()}</dialog>
  }
}}

const template = readFileSync('frontend/template.html', 'utf8')
const start = template.indexOf('<body>'), end = template.lastIndexOf('</body>')
const content = renderToStaticMarkup(<><div className="backdrop-frame"><Backdrop /></div>{parse(template.slice(start + 6, end), options)}</>)
const head = template.slice(0, start).replace('data-theme="dark"', 'data-theme="dark" class="dark"')
  .replace('./app.css?v=byok16', './chaiui.css?v=chai1')
  .replaceAll('byok16', 'chai1')
writeFileSync('web/demo.html', head + '<body>\n' + content + '\n</body></html>\n')
mkdirSync('web/chaiui', { recursive: true })
copyFileSync('public/chaiui/background.svg', 'web/chaiui/background.svg')
mkdirSync('web/files', { recursive: true })
for (const font of ['manrope', 'montserrat', 'onest', 'geist-mono']) {
  const dir = `node_modules/@fontsource-variable/${font}/files`
  for (const name of readdirSync(dir)) if (name.endsWith('.woff2')) copyFileSync(`${dir}/${name}`, `web/files/${name}`)
  copyFileSync(`node_modules/@fontsource-variable/${font}/LICENSE`, `web/files/LICENSE-${font}.txt`)
}
console.log('Built web/demo.html from ChaiUI registry components.')
