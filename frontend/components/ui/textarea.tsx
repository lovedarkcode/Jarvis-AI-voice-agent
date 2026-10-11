import * as React from 'react'

import { cn } from '@/frontend/lib/utils'
import { inputClass } from '@/frontend/components/ui/input'

/** The input's look for longer text. It grows with its content where the browser supports it. */
function Textarea({ className, ...props }: React.ComponentProps<'textarea'>) {
  return <textarea data-slot="textarea" className={cn(inputClass, 'h-auto min-h-24 resize-y py-2 leading-relaxed [field-sizing:content]', className)} {...props} />
}

export { Textarea }
