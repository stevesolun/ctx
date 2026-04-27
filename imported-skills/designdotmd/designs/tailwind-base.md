---
version: alpha
name: Tailwind Base
description: Safe, shipping-ready, infinitely remixable.
colors:
  primary: "#0F172A"
  secondary: "#64748B"
  tertiary: "#4F46E5"
  neutral: "#F1F5F9"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.75rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 2.25rem
    fontWeight: 700
    letterSpacing: "-0.02em"
  body:
    fontFamily: Inter
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.02em"
rounded:
  sm: 4px
  md: 8px
  lg: 12px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

The default you can ship on Monday. Neutral slate, indigo action, clear type hierarchy. Nothing to prove.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0F172A`):** Headlines and core text.
- **Secondary (`#64748B`):** Borders, captions, and metadata.
- **Tertiary (`#4F46E5`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1F5F9`):** The page foundation.

## Typography

- **display:** Inter 3.75rem
- **h1:** Inter 2.25rem
- **body:** Inter 0.95rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
