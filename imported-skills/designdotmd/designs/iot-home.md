---
version: alpha
name: IoT Home
description: Smart-home dashboard: warm dark, peach glow, cool teal.
colors:
  primary: "#F0EFE9"
  secondary: "#8A857A"
  tertiary: "#FFB98C"
  neutral: "#17181B"
  surface: "#1F2024"
  on-primary: "#17181B"
typography:
  display:
    fontFamily: Manrope
    fontSize: 3.75rem
    fontWeight: 600
    letterSpacing: "-0.025em"
  h1:
    fontFamily: Manrope
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: Manrope
    fontSize: 0.95rem
    lineHeight: 1.55
  label:
    fontFamily: Manrope
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.06em"
rounded:
  sm: 8px
  md: 14px
  lg: 22px
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

A smart-home dashboard: deep dark with a warm peach accent and teal status.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#F0EFE9`):** Headlines and core text.
- **Secondary (`#8A857A`):** Borders, captions, and metadata.
- **Tertiary (`#FFB98C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#17181B`):** The page foundation.

## Typography

- **display:** Manrope 3.75rem
- **h1:** Manrope 2rem
- **body:** Manrope 0.95rem
- **label:** Manrope 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
