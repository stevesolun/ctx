---
version: alpha
name: Sunset Magazine
description: Dusk over the pacific — peach, plum, bone.
colors:
  primary: "#2E1F2E"
  secondary: "#8A6A7D"
  tertiary: "#F4A27B"
  neutral: "#F8EEE3"
  surface: "#FFF8ED"
  on-primary: "#FFF8ED"
typography:
  display:
    fontFamily: Playfair Display
    fontSize: 4.75rem
    fontWeight: 500
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Playfair Display
    fontSize: 2.75rem
    fontWeight: 500
  body:
    fontFamily: Lora
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Inter
    fontSize: 0.75rem
    letterSpacing: "0.12em"
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

Editorial warmth without the gradient cliche. Flat peach, plum ink, bone paper. Feels like a glossy west-coast quarterly.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#2E1F2E`):** Headlines and core text.
- **Secondary (`#8A6A7D`):** Borders, captions, and metadata.
- **Tertiary (`#F4A27B`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F8EEE3`):** The page foundation.

## Typography

- **display:** Playfair Display 4.75rem
- **h1:** Playfair Display 2.75rem
- **body:** Lora 1.05rem
- **label:** Inter 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
