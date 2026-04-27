---
version: alpha
name: Sports HUD
description: Stadium energy. Angled badges, neon scoreline.
colors:
  primary: "#0E1016"
  secondary: "#5B6270"
  tertiary: "#00E676"
  neutral: "#F1F3F5"
  surface: "#FFFFFF"
  on-primary: "#0E1016"
typography:
  display:
    fontFamily: Archivo Black
    fontSize: 4rem
    fontWeight: 900
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Archivo
    fontSize: 2.25rem
    fontWeight: 800
  body:
    fontFamily: Archivo
    fontSize: 0.95rem
    lineHeight: 1.5
  label:
    fontFamily: Archivo
    fontSize: 0.72rem
    fontWeight: 700
    letterSpacing: "0.14em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A hard-edged broadcast-style system for sports and racing games.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0E1016`):** Headlines and core text.
- **Secondary (`#5B6270`):** Borders, captions, and metadata.
- **Tertiary (`#00E676`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F1F3F5`):** The page foundation.

## Typography

- **display:** Archivo Black 4rem
- **h1:** Archivo 2.25rem
- **body:** Archivo 0.95rem
- **label:** Archivo 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
