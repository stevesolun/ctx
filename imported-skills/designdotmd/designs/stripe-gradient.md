---
version: alpha
name: Stripe Gradient
description: Deep navy, sky, a whisper of violet.
colors:
  primary: "#0A2540"
  secondary: "#425466"
  tertiary: "#635BFF"
  neutral: "#F6F9FC"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Sohne
    fontSize: 4rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Inter
    fontSize: 2.25rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.02em"
rounded:
  sm: 4px
  md: 8px
  lg: 16px
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

Financial-grade trust with warmth. Deep navy primary, clean sans, violet-blue accent for interactive surfaces.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A2540`):** Headlines and core text.
- **Secondary (`#425466`):** Borders, captions, and metadata.
- **Tertiary (`#635BFF`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F6F9FC`):** The page foundation.

## Typography

- **display:** Sohne 4rem
- **h1:** Inter 2.25rem
- **body:** Inter 1rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
