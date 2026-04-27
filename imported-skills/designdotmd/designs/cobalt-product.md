---
version: alpha
name: Cobalt Product
description: A shippable SaaS palette with spine.
colors:
  primary: "#111827"
  secondary: "#6B7280"
  tertiary: "#2563EB"
  neutral: "#F9FAFB"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Inter
    fontSize: 3.5rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Inter
    fontSize: 2rem
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
  sm: 6px
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

Built for dashboards. Cool grey scaffolding, cobalt action, strong type hierarchy, no decoration.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#111827`):** Headlines and core text.
- **Secondary (`#6B7280`):** Borders, captions, and metadata.
- **Tertiary (`#2563EB`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F9FAFB`):** The page foundation.

## Typography

- **display:** Inter 3.5rem
- **h1:** Inter 2rem
- **body:** Inter 0.95rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
