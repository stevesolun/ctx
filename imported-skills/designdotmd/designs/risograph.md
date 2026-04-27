---
version: alpha
name: Risograph
description: Misregistered, grainy, joyful.
colors:
  primary: "#1D3FA6"
  secondary: "#6680C2"
  tertiary: "#FF4FB4"
  neutral: "#F2EEE4"
  surface: "#FAF6EC"
  on-primary: "#FAF6EC"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 4.25rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2.25rem
    fontWeight: 700
  body:
    fontFamily: Space Grotesk
    fontSize: 1rem
    lineHeight: 1.55
  label:
    fontFamily: Space Mono
    fontSize: 0.75rem
    letterSpacing: "0.04em"
rounded:
  sm: 2px
  md: 4px
  lg: 8px
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

Two-color Risograph energy: fluorescent pink over federal blue, with grainy off-white paper. Designed for joy.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1D3FA6`):** Headlines and core text.
- **Secondary (`#6680C2`):** Borders, captions, and metadata.
- **Tertiary (`#FF4FB4`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2EEE4`):** The page foundation.

## Typography

- **display:** Space Grotesk 4.25rem
- **h1:** Space Grotesk 2.25rem
- **body:** Space Grotesk 1rem
- **label:** Space Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
