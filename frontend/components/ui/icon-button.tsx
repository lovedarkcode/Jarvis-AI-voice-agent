import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'

import { cn } from '@/frontend/lib/utils'

/**
 * The size-9 square in listing toolbars (sort, filters). `boxed` has the
 * toolbar hairline; `ghost` is bare, for headers. Give it an aria-label.
 */
const iconButtonVariants = cva(
  "inline-flex size-9 shrink-0 cursor-pointer items-center justify-center rounded-md transition-colors duration-150 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 [&_svg:not([class*='size-'])]:size-4",
  {
    variants: {
      variant: {
        boxed:
          'border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 hover:text-gray-900 dark:border-gray-800 dark:bg-black/50 dark:text-gray-300 dark:hover:bg-gray-800 dark:hover:text-white',
        ghost: 'text-foreground hover:bg-accent dark:hover:bg-accent/50',
      },
    },
    defaultVariants: { variant: 'boxed' },
  },
)

function IconButton({
  className,
  variant,
  asChild = false,
  ...props
}: React.ComponentProps<'button'> & VariantProps<typeof iconButtonVariants> & { asChild?: boolean; 'aria-label': string }) {
  const Comp = asChild ? Slot : 'button'
  return <Comp data-slot="icon-button" type={asChild ? undefined : 'button'} className={cn(iconButtonVariants({ variant }), className)} {...props} />
}

export { IconButton, iconButtonVariants }
